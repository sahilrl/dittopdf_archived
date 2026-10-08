"""Embedded font program analysis (fontTools for TrueType/OpenType/CFF)."""

from __future__ import annotations

import io
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from pikepdf import Name, Stream

from dittopdf.services.pdfobj import is_obj, sha256_hex

SUBSET_RE = re.compile(r"^[A-Z]{6}\+")
MAX_FONT_BYTES = 30_000_000

NAME_IDS = {
    0: "Copyright", 1: "Family", 2: "Subfamily", 3: "Unique ID", 4: "Full name",
    5: "Version", 6: "PostScript name", 7: "Trademark", 8: "Manufacturer",
    9: "Designer", 10: "Description", 11: "Vendor URL", 12: "Designer URL",
    13: "License", 14: "License URL", 16: "Typographic family", 17: "Typographic subfamily",
}


def descriptor_of(font: Any) -> Any:
    fd = font.get("/FontDescriptor")
    if fd is None and font.get("/Subtype") == Name.Type0:
        desc = font.get("/DescendantFonts")
        if desc is not None and len(desc):
            fd = desc[0].get("/FontDescriptor")
    return fd


def program_of(fd: Any) -> tuple[str | None, Any]:
    if fd is None:
        return None, None
    for key in ("/FontFile", "/FontFile2", "/FontFile3"):
        s = fd.get(key)
        if is_obj(s, Stream):
            return key, s
    return None, None


def analyse_program(key: str, stream: Stream) -> dict[str, Any]:
    """Return a dict of facts about the embedded font program. Never raises."""
    out: dict[str, Any] = {"container": key[1:]}
    sub = stream.get("/Subtype")
    if sub is not None:
        out["FontFile3 subtype"] = str(sub)
    for k in ("/Length1", "/Length2", "/Length3"):
        if k in stream:
            out[k[1:]] = int(stream[k])
    try:
        data = stream.read_bytes()
    except Exception as e:
        out["error"] = f"could not decode font stream: {e}"
        return out
    out["Program size"] = f"{len(data):,} bytes"
    out["Program SHA-256"] = sha256_hex(data)
    if len(data) > MAX_FONT_BYTES:
        out["error"] = "font program too large to parse"
        return out
    try:
        if key == "/FontFile2" or (key == "/FontFile3" and sub == Name.OpenType):
            out.update(_sfnt(data))
        elif key == "/FontFile3":
            out.update(_cff(data))
        else:
            out.update(_type1(data, int(stream.get("/Length1", len(data)))))
    except Exception as e:
        out["error"] = f"could not parse font program: {e}"
    return out


def _sfnt(data: bytes) -> dict[str, Any]:
    from fontTools.ttLib import TTFont

    f = TTFont(io.BytesIO(data), lazy=True, fontNumber=0)
    out: dict[str, Any] = {"Format": "TrueType/OpenType (sfnt)",
                           "Tables": " ".join(sorted(f.keys()))}
    if "name" in f:
        for rec in f["name"].names:
            label = NAME_IDS.get(rec.nameID)
            if label and label not in out:
                try:
                    out[label] = rec.toUnicode()
                except Exception:
                    continue
    if "head" in f:
        head = f["head"]
        out["Font revision"] = round(head.fontRevision, 4)
        epoch = datetime(1904, 1, 1, tzinfo=timezone.utc)
        for attr, label in (("created", "Created (head)"), ("modified", "Modified (head)")):
            try:
                out[label] = (epoch + timedelta(seconds=getattr(head, attr))).isoformat()
            except (OverflowError, ValueError):
                pass
    if "OS/2" in f:
        os2 = f["OS/2"]
        out["Vendor ID"] = getattr(os2, "achVendID", "")
        out["Embedding permissions (fsType)"] = getattr(os2, "fsType", "")
        out["Weight class"] = getattr(os2, "usWeightClass", "")
    if "maxp" in f:
        out["Glyph count"] = f["maxp"].numGlyphs
    return out


def _cff(data: bytes) -> dict[str, Any]:
    from fontTools.cffLib import CFFFontSet

    cff = CFFFontSet()
    cff.decompile(io.BytesIO(data), None)
    out: dict[str, Any] = {"Format": "CFF (Compact Font Format)",
                           "Internal font name": ", ".join(cff.fontNames)}
    top = cff[cff.fontNames[0]]
    for attr in ("version", "Notice", "Copyright", "FullName", "FamilyName", "Weight"):
        val = getattr(top, attr, None)
        if val:
            out[attr] = val
    try:
        out["Glyph count"] = len(top.CharStrings)
    except Exception:
        pass
    out["CID-keyed"] = hasattr(top, "ROS")
    return out


def _type1(data: bytes, length1: int) -> dict[str, Any]:
    clear = data[: length1 or len(data)].decode("latin-1", "replace")
    out: dict[str, Any] = {"Format": "Type 1"}
    first = clear.splitlines()[0] if clear else ""
    if first.startswith("%!"):
        out["Header"] = first
    for key, rx in (("Internal font name", r"/FontName\s*/(\S+)"), ("version", r"/version\s*\((.*?)\)"),
                    ("Notice", r"/Notice\s*\((.*?)\)\s*readonly"), ("FullName", r"/FullName\s*\((.*?)\)"),
                    ("FamilyName", r"/FamilyName\s*\((.*?)\)"), ("Weight", r"/Weight\s*\((.*?)\)")):
        m = re.search(rx, clear, re.S)
        if m:
            out[key] = m.group(1)[:300]
    return out
