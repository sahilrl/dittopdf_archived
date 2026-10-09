"""Give the second PDF's resources the names the original uses for them.

Content streams refer to fonts, images, graphics states… by the name they have
in the resource dictionary (``/F1 12 Tf``, ``/Im0 Do``). Renaming a resource
therefore means rewriting two places consistently: the key in the resource
dictionary and every operand that uses it in the content streams resolved
against that dictionary. Both are done here:

* :func:`scan` tokenizes a content stream and returns the byte spans of every
  resource-name operand, so :func:`rewrite` can substitute names without
  re-serializing anything else (spacing, numbers and strings stay byte for byte);
* :func:`pair_names` matches the second PDF's resources to the original's
  (same object, then same font / image size, then the same kind in order of
  first use);
* :func:`final_names` turns those pairs into a collision-free renaming.

The caller (:mod:`copier`) decides which dictionaries can be renamed safely.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable

import pikepdf
from pikepdf import Array, Dictionary, Name, Stream

from dittopdf.services.pdfobj import is_obj

CATEGORIES = ("/Font", "/XObject", "/ExtGState", "/ColorSpace", "/Pattern", "/Shading", "/Properties")
WS = b"\x00\t\n\x0c\r "
DELIMS = b"()<>[]{}/%"
# Names an operand can carry without referring to a resource (device colour spaces, abbreviations).
RESERVED = {"/DeviceGray", "/DeviceRGB", "/DeviceCMYK", "/Pattern", "/G", "/RGB", "/CMYK", "/I", "/Indexed"}
# Only plain ASCII names are renamed, so the name in the dictionary and in the content can't be read differently.
SAFE_NAME = re.compile(r"^/[!-~]+$")
_REGULAR = re.compile(rb"[^\x00\t\n\x0c\r ()<>\[\]{}/%]*")
_NUMBER = re.compile(rb"^[+-]?(\d+\.?\d*|\.\d+)$")
_ESCAPE = re.compile(rb"#([0-9A-Fa-f]{2})")

# operator -> (category, operand position counted from the end)
OPERATORS = {b"Tf": ("/Font", 2), b"Do": ("/XObject", 1), b"gs": ("/ExtGState", 1), b"cs": ("/ColorSpace", 1),
             b"CS": ("/ColorSpace", 1), b"sh": ("/Shading", 1), b"scn": ("/Pattern", 1), b"SCN": ("/Pattern", 1),
             b"BDC": ("/Properties", 1), b"DP": ("/Properties", 1)}


class ScanError(ValueError):
    pass


@dataclass(frozen=True)
class Ref:
    cat: str
    name: str
    start: int
    end: int


def renameable(name: str) -> bool:
    return bool(SAFE_NAME.match(name)) and "#" not in name and name not in RESERVED


# ----------------------------------------------------------------------------- content streams


def scan(data: bytes) -> list[Ref]:
    """Every resource-name operand in a content stream, in order, with its byte span."""
    refs: list[Ref] = []
    operands: list[tuple] = []
    depth, i, n = 0, 0, len(data)
    while i < n:
        c = data[i]
        if c in WS:
            i += 1
        elif c == 0x25:  # % comment
            while i < n and data[i] not in b"\r\n":
                i += 1
        elif c == 0x28:  # ( literal string )
            i, nest = i + 1, 1
            while i < n and nest:
                b = data[i]
                i += 2 if b == 0x5C else 1
                nest += 1 if b == 0x28 else -1 if b == 0x29 else 0
            if nest:
                raise ScanError("unterminated string")
            if not depth:
                operands.append(("x",))
        elif c == 0x3C and data[i + 1:i + 2] == b"<":
            depth, i = depth + 1, i + 2
        elif c == 0x3C:  # <hex string>
            j = data.find(b">", i)
            if j == -1:
                raise ScanError("unterminated hex string")
            i = j + 1
            if not depth:
                operands.append(("x",))
        elif c in b"[{" or (c == 0x3E and data[i + 1:i + 2] == b">") or c in b"]}":
            opening = c in b"[{"
            i += 1 if c != 0x3E else 2
            depth += 1 if opening else -1
            if depth < 0:
                raise ScanError("unbalanced delimiters")
            if not depth and not opening:
                operands.append(("x",))
        elif c == 0x2F:  # /Name
            j = _REGULAR.match(data, i + 1).end()
            if not depth:
                raw = _ESCAPE.sub(lambda m: bytes([int(m.group(1), 16)]), data[i + 1:j])
                operands.append(("name", i, j, "/" + raw.decode("latin-1")))
            i = j
        else:
            j = _REGULAR.match(data, i).end()
            if j == i:
                raise ScanError(f"unexpected byte {data[i:i + 1]!r} at {i}")
            tok, i = data[i:j], j
            if depth or _NUMBER.match(tok) or tok in (b"true", b"false", b"null"):
                if not depth:
                    operands.append(("x",))
                continue
            if tok == b"ID":  # inline image: its dictionary is the operand list since BI
                for k in range(0, len(operands) - 1, 2):
                    key, val = operands[k], operands[k + 1]
                    if key[0] == "name" and key[3] in ("/CS", "/ColorSpace") and val[0] == "name":
                        refs.append(Ref("/ColorSpace", val[3], val[1], val[2]))
                i = _end_of_inline_data(data, i)
            elif tok in OPERATORS:
                cat, pos = OPERATORS[tok]
                if len(operands) >= pos and operands[-pos][0] == "name":
                    o = operands[-pos]
                    refs.append(Ref(cat, o[3], o[1], o[2]))
            operands = []
    if depth:
        raise ScanError("unbalanced delimiters")
    return refs


def _end_of_inline_data(data: bytes, i: int) -> int:
    """Position after the ``EI`` closing inline image data that starts after ``ID`` at ``i``."""
    k = i + 1
    while True:
        k = data.find(b"EI", k)
        if k == -1:
            raise ScanError("inline image without EI")
        if data[k - 1] in WS and (k + 2 == len(data) or data[k + 2] in WS or data[k + 2] in DELIMS):
            return k + 2
        k += 1


def parsed_refs(stream: Any) -> list[tuple[str, str]]:
    """The same references as :func:`scan`, from qpdf's parser (used to cross-check it)."""
    out: list[tuple[str, str]] = []
    for item in pikepdf.parse_content_stream(stream):
        if isinstance(item, pikepdf.ContentStreamInlineImage):
            cs = item.iimage.obj.get("/ColorSpace")
            if is_obj(cs, Name):
                out.append(("/ColorSpace", str(cs)))
            continue
        op = str(item.operator).encode()
        if op in OPERATORS:
            cat, pos = OPERATORS[op]
            ops = list(item.operands)
            if len(ops) >= pos and is_obj(ops[-pos], Name):
                out.append((cat, str(ops[-pos])))
    return out


def rewrite(data: bytes, refs: Iterable[Ref], renames: dict[str, dict[str, str]]) -> bytes:
    """``data`` with each reference renamed per ``renames[cat]``; every other byte unchanged."""
    out, pos = [], 0
    for r in refs:
        new = renames.get(r.cat, {}).get(r.name)
        if new is None:
            continue
        out += [data[pos:r.start], Name(new).unparse()]
        pos = r.end
    out.append(data[pos:])
    return b"".join(out)


def first_use(refs: Iterable[Ref | tuple[str, str]]) -> dict[str, list[str]]:
    """Names per category, in order of first use."""
    out: dict[str, list[str]] = {}
    for r in refs:
        cat, name = (r.cat, r.name) if isinstance(r, Ref) else r
        lst = out.setdefault(cat, [])
        if name not in lst:
            lst.append(name)
    return out


# ----------------------------------------------------------------------------- matching


def _kind(cat: str, o: Any) -> Any:
    """What a resource is, coarsely: only resources of the same kind are paired by position."""
    d = o.stream_dict if is_obj(o, Stream) else o
    if cat in ("/Font", "/XObject") and is_obj(d, Dictionary):
        return str(d.get("/Subtype"))
    if cat == "/ColorSpace":
        return str(o[0]) if is_obj(o, Array) and len(o) else str(o)
    if cat in ("/Pattern", "/Shading") and is_obj(d, Dictionary):
        return str(d.get("/PatternType", d.get("/ShadingType")))
    return cat


def _signature(cat: str, o: Any) -> Any:
    """A finer identity: the same font (ignoring the subset prefix) or an image of the same size."""
    d = o.stream_dict if is_obj(o, Stream) else o
    if not is_obj(d, Dictionary):
        return None
    if cat == "/Font":
        base = str(d.get("/BaseFont", ""))
        return ("font", str(d.get("/Subtype")), re.sub(r"^/[A-Z]{6}\+", "/", base)) if base else None
    if cat == "/XObject" and d.get("/Subtype") == Name.Image:
        return ("image", int(d.get("/Width", 0)), int(d.get("/Height", 0)))
    return None


def pair_names(cat: str, src: Dictionary | None, dst: Dictionary, src_order: list[str], dst_order: list[str],
               same: Callable[[Any, Any], bool]) -> dict[str, str]:
    """Second-PDF name -> original name, for resources judged to be the same one.

    Tiers, each in order of first use: the same object, the same font or image size, the same kind.
    """
    if not is_obj(src, Dictionary):
        return {}
    s_names = [k for k in dict.fromkeys(src_order + list(src.keys())) if k in src and renameable(k)]
    d_names = [k for k in dict.fromkeys(dst_order + list(dst.keys())) if k in dst and renameable(k)]
    pairs: dict[str, str] = {}
    taken: set[str] = set()
    tiers: list[Callable[[Any, Any], bool]] = [
        same,
        lambda s, d: _signature(cat, s) is not None and _signature(cat, s) == _signature(cat, d),
        lambda s, d: _kind(cat, s) == _kind(cat, d),
    ]
    for match in tiers:
        for dn in d_names:
            if dn in pairs:
                continue
            for sn in s_names:
                if sn not in taken and match(src[sn], dst[dn]):
                    pairs[dn] = sn
                    taken.add(sn)
                    break
    return pairs


def final_names(keys: Iterable[str], pairs: dict[str, str], occupied: Iterable[str] = ()) -> dict[str, str]:
    """Old name -> new name for the keys whose name changes; the result is collision-free.

    ``occupied`` are names that must not be produced (e.g. referenced by the content but
    missing from the dictionary: giving that name to a resource would change the page).
    """
    keys = list(keys)
    blocked = set(occupied)
    final: dict[str, str] = {}
    used: set[str] = set()
    for k in keys:
        t = pairs.get(k)
        if t is not None and t not in blocked and t not in used:
            final[k] = t
            used.add(t)
    for k in keys:
        if k in final:
            continue
        name, i = k, 1
        while name in used or (name in blocked and name != k):
            name, i = f"{k}_{i}", i + 1
        final[k] = name
        used.add(name)
    return {k: v for k, v in final.items() if k != v}


def rename_keys(d: Dictionary, renames: dict[str, str]) -> None:
    """Rename keys of ``d`` in place (the dictionary keeps its identity and object number)."""
    values = {old: d[old] for old in renames}
    for old in renames:
        del d[old]
    for old, new in renames.items():
        d[new] = values[old]
