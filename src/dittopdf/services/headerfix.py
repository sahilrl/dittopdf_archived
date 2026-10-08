"""Reproduce the original's exact header bytes in a file written by qpdf.

qpdf always begins its output with ``%PDF-x.y\\n%\\xbf\\xf7\\xa2\\xfe\\n`` and has
no option to change it. This module rewrites that header block after saving.
Because every object offset is relative to the start of the file, a header of a
different length moves all objects; the cross-reference data is corrected
accordingly:

* classic xref tables: each fixed-width offset is increased by the shift;
* cross-reference streams: entries are decoded, shifted and re-encoded (the
  xref stream is the last object qpdf writes, so only ``startxref`` follows it).

Linearized files are only rewritten when the lengths are equal: their hint
tables and linearization dictionary hold absolute offsets.

Every rewrite is verified by reopening the result and comparing all objects
with the unrewritten file; on any doubt the unrewritten file is kept.
"""

from __future__ import annotations

import os
import re
import zlib
from pathlib import Path

import pikepdf
from pikepdf import Name, Stream

from dittopdf.services.rawfile import header_block

STARTXREF_TAIL_RE = re.compile(rb"startxref\s+(\d+)\s*%%EOF\s*$")
XREF_ENTRY_RE = re.compile(rb"(\d{10}) (\d{5}) ([nf])")


class HeaderError(Exception):
    pass


def desired_block(original_block: bytes, version: str) -> bytes:
    """The original's header block with the version actually written."""
    m = re.match(rb"%PDF-\d+\.\d+", original_block)
    if not m:
        return original_block
    return b"%PDF-" + version.encode("ascii") + original_block[m.end():]


def rewrite_header(path: Path, desired: bytes, password: str = "") -> tuple[str, str]:
    """Make ``path`` start with ``desired``. Returns (status, message).

    status is "exact" (already identical), "rewritten" or "failed".
    """
    data = path.read_bytes()
    if not data.startswith(b"%PDF-"):
        return "failed", "The written file does not start with a %PDF header."
    current, _, _ = header_block(data, 0)
    if current == desired:
        return "exact", "The writer's header bytes already match."
    shift = len(desired) - len(current)
    try:
        if shift == 0:
            candidate = desired + data[len(current):]
        else:
            with pikepdf.open(path, password=password) as pdf:
                if pdf.is_linearized:
                    return "failed", (
                        f"The original's header block is {len(desired)} bytes and the writer's is {len(current)}. "
                        "A linearized file cannot be shifted safely (its hint tables hold absolute offsets); "
                        "turn off linearization to reproduce the header bytes.")
                candidate = _shift(data, current, desired, shift, pdf)
        problem = _verify(path, candidate, desired, password)
    except (HeaderError, pikepdf.PdfError, ValueError) as e:
        return "failed", f"Header bytes could not be reproduced: {e}"
    if problem:
        return "failed", f"Header rewrite was not applied because verification failed: {problem}"
    tmp = path.with_suffix(".hdr.tmp")
    tmp.write_bytes(candidate)
    os.replace(tmp, path)
    if shift:
        return "rewritten", (f"Header bytes reproduced exactly; all object offsets were shifted by {shift:+d} "
                             "bytes and the cross-reference data corrected.")
    return "rewritten", "Header bytes reproduced exactly (same length, no offsets moved)."


def _shift(data: bytes, current: bytes, desired: bytes, shift: int, pdf: pikepdf.Pdf) -> bytes:
    tail = STARTXREF_TAIL_RE.search(data[-64:])
    if not tail:
        raise HeaderError("no startxref at the end of the written file")
    startxref = int(tail.group(1))
    body = desired + data[len(current):]
    xref_at = startxref + shift
    if body[xref_at:xref_at + 4] == b"xref":
        return _shift_table(body, xref_at, shift)
    return _shift_stream(body, xref_at, shift, pdf)


def _set_startxref(body: bytes, value: int) -> bytes:
    pos = body.rfind(b"startxref")
    return body[:pos] + re.sub(rb"startxref\s+\d+", b"startxref\n%d" % value, body[pos:], count=1)


def _shift_table(body: bytes, xref_at: int, shift: int) -> bytes:
    trailer = body.find(b"trailer", xref_at)
    if trailer == -1:
        raise HeaderError("cross-reference table without trailer")

    def fix(m: re.Match) -> bytes:
        if m.group(3) != b"n":
            return m.group(0)
        off = int(m.group(1)) + shift
        if not 0 <= off < 10**10:
            raise HeaderError("offset out of range")
        return b"%010d %s n" % (off, m.group(2))

    table = XREF_ENTRY_RE.sub(fix, body[xref_at:trailer])
    if b"/Prev" in body[trailer:]:
        raise HeaderError("multi-section cross-reference data")
    return _set_startxref(body[:xref_at] + table + body[trailer:], xref_at)


def _shift_stream(body: bytes, xref_at: int, shift: int, pdf: pikepdf.Pdf) -> bytes:
    m = re.match(rb"(\d+) (\d+) obj", body[xref_at:xref_at + 30])
    if not m:
        raise HeaderError("startxref does not point at a cross-reference stream")
    objgen = (int(m.group(1)), int(m.group(2)))
    xs = pdf.get_object(objgen)
    if not isinstance(xs, Stream) or xs.get("/Type") != Name.XRef:
        raise HeaderError("startxref does not point at a cross-reference stream")
    if "/Prev" in xs:
        raise HeaderError("multi-section cross-reference data")
    w = [int(x) for x in xs.W]
    entries = xs.read_bytes()
    size = sum(w)
    if size == 0 or len(entries) % size:
        raise HeaderError("unexpected cross-reference stream layout")
    rows = []
    for i in range(0, len(entries), size):
        row, pos = [], i
        for width in w:
            row.append(int.from_bytes(entries[pos:pos + width], "big") if width else None)
            pos += width
        typ = row[0] if w[0] else 1
        if typ == 1:
            row[1] += shift
        rows.append(row)
    w1 = max(w[1], max(((r[1] or 0).bit_length() + 7) // 8 for r in rows))
    new_w = [w[0], w1, w[2]]
    raw = b"".join(b"".join((v or 0).to_bytes(width, "big") for v, width in zip(r, new_w) if width)
                   for r in rows)
    comp = zlib.compress(raw, 9)
    d = {k: v for k, v in xs.stream_dict.items() if k not in ("/Length", "/Filter", "/DecodeParms", "/W")}
    parts = [f"{k} ".encode() + v.unparse() if hasattr(v, "unparse") else f"{k} {v}".encode()
             for k, v in d.items()]
    parts += [b"/W [ %d %d %d ]" % tuple(new_w), b"/Filter /FlateDecode", b"/Length %d" % len(comp)]
    obj = (m.group(0) + b"\n<< " + b" ".join(parts) + b" >>\nstream\n" + comp + b"\nendstream\nendobj\n")
    return _set_startxref(body[:xref_at] + obj + b"startxref\n0\n%%EOF\n", xref_at)


def _verify(path: Path, candidate: bytes, desired: bytes, password: str) -> str | None:
    if header_block(candidate, 0)[0] != desired:
        return "the header block is not the requested one"
    tmp = path.with_suffix(".hdr.check")
    tmp.write_bytes(candidate)
    try:
        with pikepdf.open(path, password=password) as before, pikepdf.open(tmp, password=password) as after:
            if len(before.pages) != len(after.pages):
                return "page count changed"
            old = {o.objgen: o for o in before.objects if o is not None}
            new = {o.objgen: o for o in after.objects if o is not None}
            if set(old) != set(new):
                return "object set changed"
            for og, a in old.items():
                b = new[og]
                if isinstance(a, Stream):
                    if a.get("/Type") == Name.XRef:
                        continue
                    if a.read_raw_bytes() != b.read_raw_bytes() or \
                            a.stream_dict.unparse(resolved=True) != b.stream_dict.unparse(resolved=True):
                        return f"object {og[0]} {og[1]} differs"
                elif _unparse(a) != _unparse(b):
                    return f"object {og[0]} {og[1]} differs"
            warnings = after.get_warnings()
            if warnings:
                return f"the file needed repair when reopened ({warnings[0]})"
    finally:
        tmp.unlink(missing_ok=True)
    return None


def _unparse(o: object) -> bytes:
    try:
        return o.unparse(resolved=True)  # type: ignore[attr-defined]
    except Exception:
        return repr(o).encode()
