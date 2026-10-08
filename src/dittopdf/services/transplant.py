"""Deep-copy object graphs from one PDF into another, remapping references.

``Pdf.copy_foreign`` copies everything reachable from an object, including
whole pages of the source document when a bookmark or destination points at
one. To copy *metadata* structures (outlines, destinations, page labels,
document parts, annotations…) onto a different document, references to the
original's pages must instead point at the corresponding pages of the
destination. :class:`Transplanter` does that:

* references to source pages map to the destination page with the same
  number, or to null when the destination has no such page;
* other remapped objects (e.g. annotations already present in the
  destination) are substituted;
* everything else is copied once (a memo keeps shared objects shared and
  makes cycles safe), streams with their encoded bytes unchanged.
"""

from __future__ import annotations

from typing import Any

import pikepdf
from pikepdf import Array, Dictionary, Name, Pdf, Stream, String

from dittopdf.services.pdfobj import is_obj

STREAM_LENGTH_KEYS = {"/Length", "/Filter", "/DecodeParms"}


class Transplanter:
    def __init__(self, src: Pdf, dst: Pdf, *, strip_signatures: bool = True) -> None:
        self.src, self.dst = src, dst
        self.memo: dict[tuple[int, int], Any] = {}
        self.remap: dict[tuple[int, int], Any] = {}
        self.src_pages = {p.obj.objgen for p in src.pages}
        self.dropped_pages: set[int] = set()
        self.last_dropped: set[int] = set()  # pages dropped since the caller last cleared it
        self.memo_dropped: dict[tuple[int, int], set[int]] = {}
        self.stripped_signatures = 0
        self.strip_signatures = strip_signatures
        n = min(len(src.pages), len(dst.pages))
        for i in range(n):
            self.remap[src.pages[i].obj.objgen] = dst.pages[i].obj
        self.remap[src.Root.objgen] = dst.Root
        pages_root = src.Root.get("/Pages")
        if pages_root is not None and pages_root.is_indirect:
            self.remap[pages_root.objgen] = dst.Root.Pages
        self.page_numbers = {p.obj.objgen: i for i, p in enumerate(src.pages, 1)}

    def copy(self, obj: Any) -> Any:
        if obj is None or isinstance(obj, (bool, int, float)):
            return obj
        if not is_obj(obj, pikepdf.Object):
            return obj  # Decimal and other Python scalars
        if obj.is_indirect:
            og = obj.objgen
            if og in self.remap:
                return self.remap[og]
            if og in self.memo:
                self.last_dropped |= self.memo_dropped.get(og, set())
                return self.memo[og]
            if og in self.src_pages:
                n = self.page_numbers.get(og, 0)
                self.dropped_pages.add(n)
                self.last_dropped.add(n)
                return None
            if is_obj(obj, Dictionary) and obj.get("/Type") == Name.Pages:
                return self.dst.Root.Pages
            # Remember which missing pages each copied object's subtree referred to, so a
            # later reference to the same (memoized) object reports them too.
            outer, self.last_dropped = self.last_dropped, set()
            try:
                return self._copy(obj)
            finally:
                self.memo_dropped[og] = self.last_dropped
                self.last_dropped = outer | self.last_dropped
        return self._copy(obj)

    def _copy(self, obj: Any) -> Any:
        if is_obj(obj, Name):
            return Name(str(obj))
        if is_obj(obj, String):
            return String(bytes(obj))
        if is_obj(obj, Stream):
            new = Stream(self.dst, b"")
            self.memo[obj.objgen] = new
            filt, parms = obj.stream_dict.get("/Filter"), obj.stream_dict.get("/DecodeParms")
            new.write(obj.read_raw_bytes(), filter=self.copy(filt) if filt is not None else None,
                      decode_parms=self.copy(parms) if parms is not None else None, type_check=False)
            for k, v in obj.stream_dict.items():
                if k not in STREAM_LENGTH_KEYS:
                    c = self.copy(v)
                    if c is not None:
                        new[k] = c
            return new
        if is_obj(obj, Array):
            if obj.is_indirect:
                new = self.dst.make_indirect(Array())
                self.memo[obj.objgen] = new
                for v in obj:
                    new.append(self.copy(v))
                return new
            return Array([self.copy(v) for v in obj])
        if is_obj(obj, Dictionary):
            new = self.dst.make_indirect(Dictionary()) if obj.is_indirect else Dictionary()
            if obj.is_indirect:
                self.memo[obj.objgen] = new
            strip_v = self.strip_signatures and obj.get("/FT") == Name.Sig and "/V" in obj
            for k, v in obj.items():
                if strip_v and k in ("/V", "/Lock", "/SV"):
                    continue
                c = self.copy(v)
                if c is not None:
                    new[k] = c
            if strip_v:
                self.stripped_signatures += 1
            return new
        return obj
