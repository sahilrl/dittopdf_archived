"""Assign the output's object numbers so they match the original's where possible.

qpdf renumbers objects on every write, so dittopdf writes the output itself
(:mod:`pdfwriter`) using the map built here. Numbers are assigned in three
passes, in order of how meaningful the correspondence is:

1. **counterparts**: objects that *are* the original's (catalog, Info, page
   tree, pages by number, XMP, every object copied from the original) take the
   original's number and generation;
2. **structural matches**: walking mapped pairs in parallel (same dictionary
   key / array index), an output object found where the original had an
   indirect object takes that object's number (e.g. page 1's /Contents, font
   /F1, the second PDF's own /AcroForm);
3. **leftovers** take numbers the original used for objects that are not in
   the output (lowest first), then numbers above the original's highest.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable

import pikepdf
from pikepdf import Array, Dictionary, Name, Pdf, Stream

from dittopdf.services.pdfobj import is_obj

ObjGen = tuple[int, int]
TRAILER_SKIP = {"/Size", "/Root", "/Info", "/ID", "/Encrypt", "/Prev", "/XRefStm", "/Type", "/W", "/Index",
                "/Length", "/Filter", "/DecodeParms"}


@dataclass
class Numbering:
    map: dict[ObjGen, ObjGen] = field(default_factory=dict)   # dst objgen -> output (num, gen)
    kind: dict[ObjGen, str] = field(default_factory=dict)     # dst objgen -> counterpart|structural|reused|new
    reserved: set[int] = field(default_factory=set)            # original numbers kept for structural objects
    src_size: int = 0
    src_max: int = 0

    def stats(self) -> dict[str, int]:
        out = {"counterpart": 0, "structural": 0, "reused": 0, "new": 0}
        for k in self.kind.values():
            out[k] += 1
        return out


# ----------------------------------------------------------------------------- helpers


def reachable(pdf: Pdf, extra: Iterable[Any] = ()) -> list[Any]:
    """Indirect objects reachable from the trailer (excluding /Encrypt), in objgen order."""
    seen: dict[ObjGen, Any] = {}
    stack: list[Any] = []
    tr = pdf.trailer
    for k in ("/Root", "/Info"):
        if k in tr:
            stack.append(tr[k])
    for k, v in tr.items():
        if k not in TRAILER_SKIP:
            stack.append(v)
    stack.extend(extra)
    while stack:
        o = stack.pop()
        if not is_obj(o, pikepdf.Object):
            continue
        if o.is_indirect:
            if o.objgen in seen or o.objgen == (0, 0):
                continue
            seen[o.objgen] = o
        if is_obj(o, Stream):
            stack.extend(o.stream_dict.values())
        elif is_obj(o, Dictionary):
            stack.extend(o.values())
        elif is_obj(o, Array):
            stack.extend(o)
    return [seen[k] for k in sorted(seen)]


def objstm_membership(pdf: Pdf) -> dict[int, int]:
    """Object number -> number of the object stream that holds it (from the ObjStm headers)."""
    out: dict[int, int] = {}
    for o in pdf.objects:
        if not (is_obj(o, Stream) and o.stream_dict.get("/Type") == Name.ObjStm):
            continue
        try:
            n, first = int(o.stream_dict["/N"]), int(o.stream_dict["/First"])
            nums = o.read_bytes()[:first].split()
            for i in range(0, min(len(nums), 2 * n), 2):
                out.setdefault(int(nums[i]), o.objgen[0])
        except Exception:
            continue
    return out


def xref_streams(pdf: Pdf) -> list[int]:
    return sorted(o.objgen[0] for o in pdf.objects
                  if is_obj(o, Stream) and o.stream_dict.get("/Type") == Name.XRef)


# ----------------------------------------------------------------------------- map


def build_map(src: Pdf, dst: Pdf, pairs: Iterable[tuple[Any, Any]], *, reserve: Iterable[int] = ()) -> Numbering:
    """Number every object reachable in ``dst``.

    ``pairs`` are (src object, dst object) counterparts, in priority order.
    ``reserve`` are original numbers that must not be given to ordinary objects
    (object streams, cross-reference streams and /Encrypt are written there).
    """
    nb = Numbering()
    nb.reserved = set(reserve)
    # pdf.objects yields indirect numbers/booleans as plain Python values without an objgen.
    src_objgens = {o.objgen for o in src.objects if is_obj(o, pikepdf.Object) and o.objgen != (0, 0)}
    nb.src_max = max((n for n, _ in src_objgens), default=0)
    nb.src_size = max(int(src.trailer.get("/Size", 0) or 0), nb.src_max + 1)
    targets = {o.objgen: o for o in reachable(dst)}
    used: set[int] = set()

    def assign(dst_og: ObjGen, out: ObjGen, kind: str) -> bool:
        if dst_og not in targets or dst_og in nb.map or out[0] in used or out[0] in nb.reserved or out[0] <= 0:
            return False
        nb.map[dst_og] = out
        nb.kind[dst_og] = kind
        used.add(out[0])
        return True

    queue: deque = deque()
    for s, d in pairs:
        if is_obj(s, pikepdf.Object) and is_obj(d, pikepdf.Object) and s.is_indirect and d.is_indirect:
            if assign(d.objgen, s.objgen, "counterpart") or nb.map.get(d.objgen) == s.objgen:
                queue.append((s, d))

    # Structural pass: walk mapped pairs in parallel.
    visited: set[tuple[ObjGen, ObjGen]] = set()
    budget = 2_000_000
    while queue and budget > 0:
        s, d = queue.popleft()
        if s.is_indirect and d.is_indirect:
            key = (s.objgen, d.objgen)
            if key in visited:
                continue
            visited.add(key)
        for sv, dv in _children(s, d):
            budget -= 1
            if not (is_obj(dv, pikepdf.Object) and is_obj(sv, pikepdf.Object)):
                continue
            if dv.is_indirect != sv.is_indirect:
                continue
            if not dv.is_indirect:
                queue.append((sv, dv))  # direct containers: keep walking
                continue
            if _shape(sv) != _shape(dv):
                continue
            if assign(dv.objgen, sv.objgen, "structural") or nb.map.get(dv.objgen) == sv.objgen:
                queue.append((sv, dv))

    # Leftovers: free original numbers first, then above the original's range.
    free = sorted({n for n, _ in src_objgens} - used - nb.reserved)
    nxt = max(nb.src_max, max(nb.reserved, default=0)) + 1
    fi = 0
    for og in sorted(targets):
        if og in nb.map:
            continue
        if fi < len(free):
            nb.map[og], nb.kind[og] = (free[fi], 0), "reused"
            fi += 1
        else:
            nb.map[og], nb.kind[og] = (nxt, 0), "new"
            nxt += 1
        used.add(nb.map[og][0])
    return nb


def _shape(o: Any) -> str:
    if is_obj(o, Stream):
        return "stream"
    if is_obj(o, Dictionary):
        return "dict"
    if is_obj(o, Array):
        return "array"
    return "other"


def _children(s: Any, d: Any) -> Iterable[tuple[Any, Any]]:
    if is_obj(s, Stream) and is_obj(d, Stream):
        s, d = s.stream_dict, d.stream_dict
    if is_obj(s, Dictionary) and is_obj(d, Dictionary):
        for k in d.keys():
            if k in s and k not in ("/Parent", "/P"):
                yield s.get(k), d.get(k)
    elif is_obj(s, Array) and is_obj(d, Array):
        for sv, dv in zip(s, d):
            yield sv, dv
