"""Read objects straight from a PDF file's bytes, keeping their exact serialization.

qpdf parses objects into a normalized form (dictionary keys sorted, numbers and
strings re-formatted), so the original bytes of an object cannot be recovered
through pikepdf. This module reads them directly:

* :class:`RawFile` walks every cross-reference section (tables and streams, all
  revisions; the newest entry wins) and locates each object, including objects
  inside object streams;
* :func:`parse_value` tokenizes PDF syntax into :class:`Node` trees that keep the
  byte span of every value, the key order of dictionaries and the original token
  text of every scalar;
* :meth:`RawFile.style` describes how the file was written (line endings,
  compact or spaced delimiters, separators around ``obj``/``stream``), so new
  objects can be written the same way;
* :meth:`RawFile.physical_order` gives object numbers in file order.

Parsing is defensive: anything unexpected yields ``None`` and the caller falls
back to re-serializing the object.
"""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

WS = b"\x00\t\n\x0c\r "
DELIM = b"()<>[]{}/%"
REGULAR_END = WS + DELIM
STARTXREF_RE = re.compile(rb"startxref\s+(\d+)")
OBJ_HEADER_RE = re.compile(rb"(\d+)\s+(\d+)\s+obj")
NUMBER_RE = re.compile(rb"^[+-]?(\d+\.?\d*|\.\d+)$")
ESCAPES = {ord("n"): b"\n", ord("r"): b"\r", ord("t"): b"\t", ord("b"): b"\b", ord("f"): b"\f",
           ord("("): b"(", ord(")"): b")", ord("\\"): b"\\"}


class RawError(Exception):
    pass


# ----------------------------------------------------------------------------- nodes


@dataclass
class Node:
    """One parsed value. ``start:end`` is its span in ``buf``; ``ws`` the bytes before it."""
    kind: str                      # dict array name string number ref bool null keyword
    buf: bytes
    start: int
    end: int
    ws: bytes = b""
    value: Any = None              # decoded scalar: '/Name', bytes, int/Decimal, (num, gen), bool
    items: list = field(default_factory=list)   # dict: [(key Node, value Node)]; array: [Node]
    hex: bool = False              # string written as <hex>

    @property
    def raw(self) -> bytes:
        return self.buf[self.start:self.end]

    def get(self, key: str) -> "Node | None":
        for k, v in self.items:
            if k.value == key:
                return v
        return None

    def keys(self) -> list[str]:
        return [k.value for k, _ in self.items]


@dataclass
class RawObject:
    num: int
    gen: int
    buf: bytes
    start: int                     # of "N G obj" (top-level) or of the value (in an object stream)
    end: int                       # after "endobj" (top-level) or end of the value
    value: Node
    in_objstm: int | None = None   # number of the object stream holding it
    data_start: int | None = None  # stream data span
    data_end: int | None = None
    reliable: bool = True          # stream /Length matched an endstream keyword

    @property
    def raw(self) -> bytes:
        return self.buf[self.start:self.end]

    @property
    def data(self) -> bytes | None:
        if self.data_start is None:
            return None
        return self.buf[self.data_start:self.data_end]


# ----------------------------------------------------------------------------- lexer / parser


class Lexer:
    def __init__(self, buf: bytes, pos: int = 0, end: int | None = None):
        self.buf, self.pos, self.end = buf, pos, len(buf) if end is None else end

    def skip_ws(self) -> bytes:
        start, buf = self.pos, self.buf
        while self.pos < self.end:
            c = buf[self.pos]
            if c in WS:
                self.pos += 1
            elif c == 0x25:  # % comment
                while self.pos < self.end and buf[self.pos] not in (0x0A, 0x0D):
                    self.pos += 1
            else:
                break
        return buf[start:self.pos]

    def regular(self) -> bytes:
        start = self.pos
        while self.pos < self.end and self.buf[self.pos] not in REGULAR_END:
            self.pos += 1
        return self.buf[start:self.pos]


def _decode_name(raw: bytes) -> str:
    out, i = bytearray(), 1
    while i < len(raw):
        if raw[i] == 0x23 and i + 2 < len(raw) and re.fullmatch(rb"[0-9A-Fa-f]{2}", raw[i + 1:i + 3]):
            out.append(int(raw[i + 1:i + 3], 16))
            i += 3
        else:
            out.append(raw[i])
            i += 1
    return "/" + out.decode("latin-1")


def _literal_string(lx: Lexer) -> bytes:
    buf, out, depth = lx.buf, bytearray(), 1
    lx.pos += 1
    while lx.pos < lx.end:
        c = buf[lx.pos]
        if c == 0x5C:  # backslash
            lx.pos += 1
            if lx.pos >= lx.end:
                break
            e = buf[lx.pos]
            if e in ESCAPES:
                out += ESCAPES[e]
                lx.pos += 1
            elif 0x30 <= e <= 0x37:
                digits = re.match(rb"[0-7]{1,3}", buf[lx.pos:lx.pos + 3]).group(0)
                out.append(int(digits, 8) & 0xFF)
                lx.pos += len(digits)
            elif e == 0x0D:
                lx.pos += 2 if buf[lx.pos + 1:lx.pos + 2] == b"\n" else 1
            elif e == 0x0A:
                lx.pos += 1
            else:
                out.append(e)
                lx.pos += 1
            continue
        if c == 0x28:
            depth += 1
        elif c == 0x29:
            depth -= 1
            if depth == 0:
                lx.pos += 1
                return bytes(out)
        elif c == 0x0D:  # an unescaped EOL in a string is read as \n
            out.append(0x0A)
            lx.pos += 2 if buf[lx.pos + 1:lx.pos + 2] == b"\n" else 1
            continue
        out.append(c)
        lx.pos += 1
    raise RawError("unterminated string")


def parse_value(lx: Lexer, depth: int = 0) -> Node:
    """Parse one value at the lexer position (references are recognised)."""
    if depth > 200:
        raise RawError("nesting too deep")
    ws = lx.skip_ws()
    buf, start = lx.buf, lx.pos
    if start >= lx.end:
        raise RawError("unexpected end of data")
    c = buf[start]
    if buf.startswith(b"<<", start):
        lx.pos += 2
        node = Node("dict", buf, start, start, ws)
        while True:
            inner_ws = lx.skip_ws()
            if buf.startswith(b">>", lx.pos):
                lx.pos += 2
                node.end = lx.pos
                node.value = inner_ws  # whitespace before ">>"
                return node
            lx.pos -= len(inner_ws)
            key = parse_value(lx, depth + 1)
            if key.kind != "name":
                raise RawError("dictionary key is not a name")
            node.items.append((key, parse_value(lx, depth + 1)))
    if c == 0x5B:  # [
        lx.pos += 1
        node = Node("array", buf, start, start, ws)
        while True:
            inner_ws = lx.skip_ws()
            if lx.pos < lx.end and buf[lx.pos] == 0x5D:
                lx.pos += 1
                node.end = lx.pos
                node.value = inner_ws
                return node
            lx.pos -= len(inner_ws)
            node.items.append(parse_value(lx, depth + 1))
    if c == 0x28:
        value = _literal_string(lx)
        return Node("string", buf, start, lx.pos, ws, value)
    if c == 0x3C:
        close = buf.find(b">", start)
        if close == -1 or close >= lx.end:
            raise RawError("unterminated hex string")
        digits = re.sub(rb"[^0-9A-Fa-f]", b"", buf[start + 1:close])
        if len(digits) % 2:
            digits += b"0"
        lx.pos = close + 1
        return Node("string", buf, start, lx.pos, ws, bytes.fromhex(digits.decode()), hex=True)
    if c == 0x2F:
        lx.pos += 1
        tok = b"/" + lx.regular()
        return Node("name", buf, start, lx.pos, ws, _decode_name(tok))
    tok = lx.regular()
    if not tok:
        raise RawError(f"unexpected byte {chr(c)!r}")
    if NUMBER_RE.match(tok):
        node = Node("number", buf, start, lx.pos, ws, _number(tok))
        # "N G R" reference?
        if isinstance(node.value, int) and node.value >= 0:
            save = lx.pos
            ws2 = lx.skip_ws()
            gen_tok = lx.regular()
            if ws2 and gen_tok.isdigit():
                ws3 = lx.skip_ws()
                r = lx.regular()
                if ws3 and r == b"R":
                    return Node("ref", buf, start, lx.pos, ws, (node.value, int(gen_tok)))
            lx.pos = save
        return node
    if tok in (b"true", b"false"):
        return Node("bool", buf, start, lx.pos, ws, tok == b"true")
    if tok == b"null":
        return Node("null", buf, start, lx.pos, ws, None)
    return Node("keyword", buf, start, lx.pos, ws, tok.decode("latin-1"))


def _number(tok: bytes) -> int | Decimal:
    if b"." not in tok:
        return int(tok)
    try:
        return Decimal(tok.decode())
    except InvalidOperation as e:
        raise RawError(f"bad number {tok!r}") from e


# ----------------------------------------------------------------------------- filters


def _png_unpredict(data: bytes, columns: int) -> bytes:
    row, prev, out, i = columns + 1, bytearray(columns), bytearray(), 0
    while i + row <= len(data):
        ft, line = data[i], bytearray(data[i + 1:i + row])
        for x in range(columns):
            a = line[x - 1] if x else 0
            b, c = prev[x], prev[x - 1] if x else 0
            if ft == 1:
                line[x] = (line[x] + a) & 0xFF
            elif ft == 2:
                line[x] = (line[x] + b) & 0xFF
            elif ft == 3:
                line[x] = (line[x] + ((a + b) >> 1)) & 0xFF
            elif ft == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 0xFF
        out += line
        prev = line
        i += row
    return bytes(out)


def flate_decode(data: bytes, parms: Node | None) -> bytes:
    out = zlib.decompressobj().decompress(data)
    if parms is not None and parms.kind == "dict":
        pred = parms.get("/Predictor")
        if pred is not None and isinstance(pred.value, int) and pred.value >= 10:
            cols = parms.get("/Columns")
            out = _png_unpredict(out, cols.value if cols is not None else 1)
    return out


def zlib_level(compressed: bytes, plain: bytes) -> int | None:
    """The zlib level that turns ``plain`` into exactly ``compressed`` (None if none does)."""
    for level in (6, 9, 1, 5, 4, 3, 2, 7, 8, 0):
        if zlib.compress(plain, level) == compressed:
            return level
    return None


# ----------------------------------------------------------------------------- style


@dataclass
class Style:
    eol: bytes = b"\n"
    compact: bool = False                # "<</Type/Catalog>>" rather than "<< /Type /Catalog >>"
    after_obj: bytes = b"\n"             # between "obj" and the value
    before_stream: bytes = b"\n"         # between the stream dictionary and "stream"
    stream_eol: bytes = b"\n"            # after "stream"
    before_endstream: bytes = b"\n"
    before_endobj: bytes = b"\n"
    after_endobj: bytes = b"\n"
    xref_entry_eol: bytes = b"\r\n"      # the two bytes ending a 20-byte xref entry
    zlib_level: int | None = None        # level the producer used for Flate streams, if detected


# ----------------------------------------------------------------------------- the file


class RawFile:
    """Locate and parse objects in a PDF file's bytes."""

    def __init__(self, data: bytes, *, decrypt: Callable[[str, bytes, int, int], bytes] | None = None,
                 encrypt_ref: tuple[int, int] | None = None):
        self.data = data
        self.decrypt = decrypt            # (kind "string"|"stream", data, num, gen) -> plaintext
        self.encrypt_ref = encrypt_ref    # the /Encrypt dictionary is never encrypted
        self.entries: dict[int, tuple] = {}   # num -> ("n", offset, gen) | ("c", objstm, index)
        self.trailers: list[Node] = []        # newest first
        self.xref_kinds: list[str] = []       # "table" / "stream", newest first
        self._cache: dict[int, RawObject | None] = {}
        self._objstm: dict[int, tuple[bytes, list[tuple[int, int]], int] | None] = {}
        try:
            self._read_xref()
        except Exception:
            self.entries = {}

    # -- cross-reference data ------------------------------------------------------------

    def _read_xref(self) -> None:
        found = STARTXREF_RE.findall(self.data[-2048:]) or STARTXREF_RE.findall(self.data)
        if not found:
            return
        offset: int | None = int(found[-1])
        seen: set[int] = set()
        while offset is not None and offset not in seen and len(seen) < 500:
            seen.add(offset)
            if self.data[offset:offset + 4] == b"xref":
                trailer, xrefstm = self._read_table(offset)
                if xrefstm is not None:
                    self._read_stream_section(xrefstm)
            else:
                trailer = self._read_stream_section(offset)
            if trailer is None:
                break
            self.trailers.append(trailer)
            prev = trailer.get("/Prev")
            offset = prev.value if prev is not None and isinstance(prev.value, int) else None

    def _read_table(self, offset: int) -> tuple[Node | None, int | None]:
        pos = offset + 4
        t = self.data.find(b"trailer", pos)
        if t == -1:
            return None, None
        lines = self.data[pos:t].split()
        i = 0
        while i + 1 < len(lines):
            first, count = int(lines[i]), int(lines[i + 1])
            i += 2
            for n in range(first, first + count):
                if i + 2 >= len(lines):
                    break
                off, gen, typ = int(lines[i]), int(lines[i + 1]), lines[i + 2]
                i += 3
                if n not in self.entries:
                    self.entries[n] = ("n", off, gen) if typ == b"n" else ("f", 0, gen)
        self.xref_kinds.append("table")
        self._entry_eol_sample = self.data[pos:t]
        trailer = parse_value(Lexer(self.data, t + len(b"trailer")))
        stm = trailer.get("/XRefStm")
        return trailer, stm.value if stm is not None and isinstance(stm.value, int) else None

    def _read_stream_section(self, offset: int) -> Node | None:
        obj = self._parse_top(offset, None, None)
        if obj is None or obj.value.kind != "dict" or obj.data is None:
            return None
        d = obj.value
        w = [n.value for n in d.get("/W").items]
        size = d.get("/Size").value
        index = [n.value for n in d.get("/Index").items] if d.get("/Index") is not None else [0, size]
        data = flate_decode(obj.data, d.get("/DecodeParms")) if d.get("/Filter") is not None else obj.data
        pos = 0
        for first, count in zip(index[::2], index[1::2]):
            for n in range(first, first + count):
                fields = []
                for width in w:
                    fields.append(int.from_bytes(data[pos:pos + width], "big") if width else None)
                    pos += width
                typ = fields[0] if w[0] else 1
                if n in self.entries:
                    continue
                if typ == 1:
                    self.entries[n] = ("n", fields[1], fields[2] or 0)
                elif typ == 2:
                    self.entries[n] = ("c", fields[1], fields[2] or 0)
                else:
                    self.entries[n] = ("f", 0, fields[2] or 0)
                if pos > len(data):
                    break
        self.xref_kinds.append("stream")
        self.xref_stream_nums = getattr(self, "xref_stream_nums", []) + [obj.num]
        # An xref stream need not list itself, so keep the parsed object (newest first).
        self.xref_stream_objs = getattr(self, "xref_stream_objs", []) + [obj]
        return d

    # -- objects ---------------------------------------------------------------------------

    def get(self, num: int) -> RawObject | None:
        if num in self._cache:
            return self._cache[num]
        entry = self.entries.get(num)
        obj = None
        try:
            if entry and entry[0] == "n":
                obj = self._parse_top(entry[1], num, entry[2])
            elif entry and entry[0] == "c":
                obj = self._parse_compressed(num, entry[1], entry[2])
        except (RawError, ValueError, IndexError, zlib.error, AttributeError, TypeError):
            obj = None
        self._cache[num] = obj
        return obj

    def _parse_top(self, offset: int, num: int | None, gen: int | None) -> RawObject | None:
        m = OBJ_HEADER_RE.match(self.data, offset)
        if not m:
            return None
        n, g = int(m.group(1)), int(m.group(2))
        if num is not None and (n != num or g != gen):
            return None
        lx = Lexer(self.data, m.end())
        value = parse_value(lx)
        obj = RawObject(n, g, self.data, offset, lx.pos, value)
        after_value = lx.pos
        ws = lx.skip_ws()
        if self.data.startswith(b"stream", lx.pos):
            p = lx.pos + 6
            if self.data[p:p + 2] == b"\r\n":
                p += 2
            elif self.data[p:p + 1] in (b"\n", b"\r"):
                p += 1
            length = value.get("/Length") if value.kind == "dict" else None
            ln = None
            if length is not None and length.kind == "number":
                ln = length.value
            elif length is not None and length.kind == "ref":
                ref = self.get(length.value[0])
                ln = ref.value.value if ref is not None and ref.value.kind == "number" else None
            es = self.data.find(b"endstream", p + (ln or 0) if ln is not None and p + ln <= len(self.data) else p)
            if es == -1:
                return None
            if ln is not None and self.data[p + ln:es].strip(WS) == b"":
                data_end = p + ln
            else:
                obj.reliable = False
                data_end = es
                while data_end > p and self.data[data_end - 1] in b"\r\n":
                    data_end -= 1
            obj.data_start, obj.data_end = p, data_end
            lx.pos = es + 9
            after_value = lx.pos
            ws = lx.skip_ws()
        del after_value, ws
        if not self.data.startswith(b"endobj", lx.pos):
            return None
        obj.end = lx.pos + 6
        # Include the spacing and line ending after endobj in the span (part of the object's layout),
        # e.g. "endobj\n", "endobj\r\n" or "endobj \n".
        while self.data[obj.end:obj.end + 1] in (b" ", b"\t"):
            obj.end += 1
        if self.data[obj.end:obj.end + 2] == b"\r\n":
            obj.end += 2
        elif self.data[obj.end:obj.end + 1] in (b"\n", b"\r"):
            obj.end += 1
        if self.decrypt is not None and (n, g) != self.encrypt_ref:
            _decrypt_strings(obj.value, lambda b: self.decrypt("string", b, n, g))
        return obj

    def _objstm_data(self, k: int) -> tuple[bytes, list[tuple[int, int]], int] | None:
        if k in self._objstm:
            return self._objstm[k]
        res = None
        entry = self.entries.get(k)
        if entry and entry[0] == "n":
            obj = self._parse_top(entry[1], k, entry[2])
            if obj is not None and obj.data is not None:
                data = obj.data
                if self.decrypt is not None:
                    data = self.decrypt("stream", data, k, entry[2])
                d = obj.value
                if d.get("/Filter") is not None:
                    data = flate_decode(data, d.get("/DecodeParms"))
                first, n = d.get("/First").value, d.get("/N").value
                nums = data[:first].split()
                pairs = [(int(nums[i]), int(nums[i + 1])) for i in range(0, 2 * n, 2)]
                res = (data, pairs, first)
        self._objstm[k] = res
        return res

    def _parse_compressed(self, num: int, k: int, index: int) -> RawObject | None:
        st = self._objstm_data(k)
        if st is None:
            return None
        data, pairs, first = st
        if index >= len(pairs) or pairs[index][0] != num:
            return None
        start = first + pairs[index][1]
        end = first + pairs[index + 1][1] if index + 1 < len(pairs) else len(data)
        lx = Lexer(data, start, end)
        value = parse_value(lx)
        return RawObject(num, 0, data, value.start, value.end, value, in_objstm=k)

    def objstm_members(self, k: int) -> list[int]:
        st = self._objstm_data(k)
        return [n for n, _ in st[1]] if st else []

    def objstm_plain(self, k: int) -> bytes | None:
        st = self._objstm_data(k)
        return st[0] if st else None

    # -- layout ----------------------------------------------------------------------------

    def physical_order(self) -> list[int]:
        """Object numbers in the order they appear in the file."""
        top = sorted((e[1], n) for n, e in self.entries.items() if e[0] == "n")
        compressed: dict[int, list[tuple[int, int]]] = {}
        for n, e in self.entries.items():
            if e[0] == "c":
                compressed.setdefault(e[1], []).append((e[2], n))
        out = []
        for _, n in top:
            out.append(n)
            for _, m in sorted(compressed.pop(n, [])):
                out.append(m)
        for rest in compressed.values():
            out.extend(m for _, m in sorted(rest))
        return out

    def style(self) -> Style:
        st = Style()
        objs = [self.get(n) for n in self.physical_order()[:60]]
        objs = [o for o in objs if o is not None and o.in_objstm is None]
        if objs:
            o = objs[0]
            hdr = OBJ_HEADER_RE.match(o.buf, o.start)
            st.after_obj = o.buf[hdr.end():o.value.start]
            tail = o.buf[o.start:o.end]
            m = re.search(rb"([\r\n ]*)endobj([ \t]*[\r\n]*)$", tail)
            if m:
                st.before_endobj, st.after_endobj = m.group(1), m.group(2)
            st.eol = b"\r\n" if b"\r\n" in st.after_endobj + st.after_obj else (b"\r" if b"\r" in st.after_endobj
                                                                                 else b"\n")
        compact = spaced = 0
        for o in objs:
            for node in _walk(o.value):
                if node.kind == "dict":
                    if o.buf.startswith(b"<</", node.start):
                        compact += 1
                    elif o.buf.startswith(b"<< /", node.start) or o.buf.startswith(b"<<\n", node.start):
                        spaced += 1
        st.compact = compact > spaced
        streams = [o for o in objs if o.data_start is not None and o.reliable]
        if streams:
            s = streams[0]
            seg = s.buf[s.value.end:s.data_start]   # e.g. "stream\n" or "\nstream\r\n"
            i = seg.find(b"stream")
            st.before_stream, st.stream_eol = seg[:i], seg[i + 6:]
            m = re.match(rb"([\r\n ]*)endstream", s.buf[s.data_end:s.end])
            if m:
                st.before_endstream = m.group(1)
            for s in streams[:20]:
                f = s.value.get("/Filter")
                if f is not None and f.value == "/FlateDecode" and s.value.get("/DecodeParms") is None:
                    try:
                        data = s.data if self.decrypt is None else self.decrypt("stream", s.data, s.num, s.gen)
                        level = zlib_level(data, zlib.decompress(data))
                    except Exception:
                        level = None
                    if level is not None:
                        st.zlib_level = level
                        break
        sample = getattr(self, "_entry_eol_sample", b"")
        m = re.search(rb"\d{10} \d{5} [nf](\r\n| \n| \r)", sample)
        if m:
            st.xref_entry_eol = m.group(1)
        return st

    def stream_plain_raw(self, obj: RawObject) -> bytes | None:
        """The stream's encoded bytes, decrypted if the file is encrypted."""
        if obj.data is None:
            return None
        if self.decrypt is None or obj.in_objstm is not None:
            return obj.data
        return self.decrypt("stream", obj.data, obj.num, obj.gen)

    def trailer(self) -> Node | None:
        return self.trailers[0] if self.trailers else None


def _walk(node: Node):
    yield node
    if node.kind == "dict":
        for _, v in node.items:
            yield from _walk(v)
    elif node.kind == "array":
        for v in node.items:
            yield from _walk(v)


def _decrypt_strings(node: Node, fn: Callable[[bytes], bytes]) -> None:
    for n in _walk(node):
        if n.kind == "string":
            try:
                n.value = fn(n.value)
            except Exception:
                pass
