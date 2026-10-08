"""Byte-level analysis that pikepdf/qpdf does not expose.

qpdf presents the *resolved* document; it does not report how many revisions
(incremental updates) a file has, where its cross-reference sections are, or
what the header line says. These are read straight from the bytes. The results
are diagnostic only: a PDF writer always regenerates them.
"""

from __future__ import annotations

import re
from typing import Any

HEADER_RE = re.compile(rb"%PDF-(\d+\.\d+)")
STARTXREF_RE = re.compile(rb"startxref\s+(\d+)")
OBJ_RE = re.compile(rb"\s*(\d+)\s+(\d+)\s+obj")
INT_KEY = lambda key: re.compile(rb"/" + key + rb"\s+(\d+)")  # noqa: E731


def analyse(data: bytes) -> dict[str, Any]:
    head = data[:1024]
    m = HEADER_RE.search(head)
    out: dict[str, Any] = {
        "size": len(data),
        "header_version": m.group(1).decode() if m else None,
        "header_offset": m.start() if m else None,
        "binary_marker": None,
        "eof_markers": data.count(b"%%EOF"),
        "startxref": [int(x) for x in STARTXREF_RE.findall(data)],
        "sections": [],
        "chain_error": None,
        "objstm_count": len(re.findall(rb"/Type\s*/ObjStm", data)),
        "xref_streams": len(re.findall(rb"/Type\s*/XRef\b", data)),
        "trailing_bytes": 0,
    }
    out["header_block"] = out["header_eol"] = out["marker"] = None
    if m:
        block, eol, marker = header_block(data, m.start())
        out["header_block"], out["header_eol"], out["marker"] = block.hex(), eol.hex(), marker.hex()
        if marker:
            out["binary_marker"] = marker.hex()
    last_eof = data.rfind(b"%%EOF")
    if last_eof != -1:
        out["trailing_bytes"] = len(data[last_eof + 5:].strip())
    if out["startxref"]:
        _walk_chain(data, out["startxref"][-1], out)
    return out


def _line(data: bytes, pos: int) -> tuple[int, bytes]:
    """End of the line starting at ``pos`` (after its EOL) and the EOL bytes."""
    i = pos
    while i < len(data) and data[i] not in (0x0A, 0x0D):
        i += 1
    if data[i:i + 2] == b"\r\n":
        return i + 2, b"\r\n"
    if i < len(data):
        return i + 1, data[i:i + 1]
    return i, b""


def header_block(data: bytes, start: int) -> tuple[bytes, bytes, bytes]:
    """The header line plus the comment lines directly after it (the binary marker).

    Returns (block, EOL of the header line, comment lines). All bytes are exact,
    including line endings, so the block can be reproduced byte for byte.
    """
    end, eol = _line(data, start)
    pos = end
    while pos < len(data) and pos - start < 1024 and data[pos:pos + 1] == b"%" \
            and not data.startswith(b"%%EOF", pos):
        pos, _ = _line(data, pos)
    return data[start:pos], eol, data[end:pos]


def _walk_chain(data: bytes, offset: int, out: dict) -> None:
    """Follow the /Prev chain from the last startxref."""
    base = out["header_offset"] or 0
    seen = set()
    while offset is not None and len(seen) < 500:
        if offset in seen:
            out["chain_error"] = f"/Prev loop at offset {offset}"
            return
        seen.add(offset)
        pos = offset + base if not _looks_like_xref(data, offset) and _looks_like_xref(data, offset + base) else offset
        if pos >= len(data):
            out["chain_error"] = f"offset {offset} is beyond end of file"
            return
        chunk = data[pos: pos + 20]
        section: dict[str, Any] = {"offset": offset}
        if chunk.lstrip().startswith(b"xref"):
            section["type"] = "table"
            t = data.find(b"trailer", pos)
            end = data.find(b"startxref", t) if t != -1 else -1
            trailer = data[t: end if end != -1 else t + 4096] if t != -1 else b""
        elif OBJ_RE.match(chunk):
            section["type"] = "stream"
            end = data.find(b"stream", pos)
            trailer = data[pos: end if end != -1 else pos + 4096]
        else:
            out["chain_error"] = f"no cross-reference section at offset {offset}"
            return
        for key in (b"Prev", b"XRefStm", b"Size"):
            km = INT_KEY(key).search(trailer)
            if km:
                section[key.decode()] = int(km.group(1))
        out["sections"].append(section)
        offset = section.get("Prev")


def _looks_like_xref(data: bytes, pos: int) -> bool:
    chunk = data[pos: pos + 20]
    return chunk.lstrip().startswith(b"xref") or bool(OBJ_RE.match(chunk))
