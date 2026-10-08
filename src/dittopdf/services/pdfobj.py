"""Conversions between pikepdf objects and JSON-friendly values.

Three representations are produced for every inspected value:

* ``brief``: a short human-readable rendering for tables;
* ``to_json``: a typed JSON form (the qpdf JSON v2 conventions) that the
  edit screen uses and :func:`from_json` parses back. Strings are ``"u:text"``
  or ``"b:hex"``, names ``"/Name"``, indirect references ``"12 0 R"``;
* ``canonical``: a digest used to decide whether two values are the same.
  Indirect references are followed and page references are replaced by page
  numbers, so equal structures in two different files compare equal even
  though their object numbers differ.
"""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal
from typing import Any, Callable

import pikepdf
from pikepdf import Array, Dictionary, Name, Stream, String

PDF_DATE_RE = re.compile(
    r"^(?:D:)?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?"
    r"(?:([Zz])(?:00'?(?:00'?)?)?|([+\-])(\d{2})'?(?:(\d{2})'?)?)?$"
)
REF_RE = re.compile(r"^(\d+) (\d+) R$")
HEX_RE = re.compile(r"^[0-9a-fA-F\s]*$")


# --------------------------------------------------------------------------- dates


def is_pdf_date(text: str) -> bool:
    return bool(PDF_DATE_RE.match(text.strip()))


def pdf_date_to_iso(text: str) -> str | None:
    """``D:20240102030405+01'00'`` -> ``2024-01-02T03:04:05+01:00`` (for display only)."""
    m = PDF_DATE_RE.match(text.strip())
    if not m:
        return None
    y, mo, d, h, mi, s, z, sign, tzh, tzm = m.groups()
    out = f"{y}-{mo or '01'}-{d or '01'}T{h or '00'}:{mi or '00'}:{s or '00'}"
    if z:
        out += "Z"
    elif sign:
        out += f"{sign}{tzh}:{tzm or '00'}"
    return out


# --------------------------------------------------------------------------- strings


def decode_text(raw: bytes) -> str | None:
    """Decode a PDF text string, or return None if it does not look like text."""
    try:
        if raw.startswith(b"\xfe\xff"):
            text = raw[2:].decode("utf-16-be")
        elif raw.startswith(b"\xef\xbb\xbf"):
            text = raw[3:].decode("utf-8")
        else:
            text = raw.decode("pdfdoc")
    except (UnicodeDecodeError, LookupError):
        return None
    if all(ch.isprintable() or ch in "\n\r\t" for ch in text):
        return text
    return None


def encode_text(text: str, like: bytes | None = None) -> String:
    """Encode ``text`` as a PDF string, keeping the encoding style of ``like``."""
    if like is not None and like.startswith(b"\xfe\xff"):
        return String(b"\xfe\xff" + text.encode("utf-16-be"))
    if like is not None and like.startswith(b"\xef\xbb\xbf"):
        return String(b"\xef\xbb\xbf" + text.encode("utf-8"))
    try:
        return String(text.encode("pdfdoc"))
    except UnicodeEncodeError:
        return String(b"\xfe\xff" + text.encode("utf-16-be"))


# --------------------------------------------------------------------------- kinds


def is_obj(o: Any, cls: type) -> bool:
    try:
        return isinstance(o, cls)
    except Exception:
        return False


def kind_of(obj: Any) -> str:
    """Classify a value for the edit screen's choice of control."""
    if obj is None:
        return "null"
    if isinstance(obj, bool):
        return "bool"
    if isinstance(obj, (int, float, Decimal)):
        return "number"
    if is_obj(obj, Name):
        return "name"
    if is_obj(obj, String):
        raw = bytes(obj)
        text = decode_text(raw)
        if text is None:
            return "binary"
        return "date" if is_pdf_date(text) else "text"
    if is_obj(obj, Stream):
        return "stream"
    if is_obj(obj, Array) or is_obj(obj, Dictionary):
        return "object"
    return "other"


def ref_str(obj: pikepdf.Object) -> str:
    num, gen = obj.objgen
    return f"{num} {gen} R"


# --------------------------------------------------------------------------- typed JSON


def to_json(obj: Any, *, deref: int = 0, pages: dict | None = None, _top: bool = True,
            _seen: frozenset = frozenset()) -> Any:
    """Typed JSON. Indirect children are emitted as ``"N G R"`` unless ``deref`` > 0.

    With ``pages`` (objgen -> page number), page references are never followed;
    they render as ``"N G R"`` followed by the page number (for display only).
    """
    if obj is None:
        return None
    if isinstance(obj, bool) or isinstance(obj, int):
        return obj
    if isinstance(obj, (Decimal, float)):
        f = float(obj)
        return int(f) if f.is_integer() and "." not in str(obj) else f
    if is_obj(obj, pikepdf.Object) and obj.is_indirect and not _top:
        if pages and obj.objgen in pages:
            return f"{ref_str(obj)} (page {pages[obj.objgen]})"
        if deref <= 0 or obj.objgen in _seen:
            return ref_str(obj)
        deref -= 1
        _seen = _seen | {obj.objgen}
    if is_obj(obj, Name):
        return str(obj)
    if is_obj(obj, String):
        raw = bytes(obj)
        text = decode_text(raw)
        return f"u:{text}" if text is not None else f"b:{raw.hex()}"
    if is_obj(obj, Stream):
        d = {str(k): to_json(v, deref=deref, pages=pages, _top=False, _seen=_seen) for k, v in obj.stream_dict.items()}
        return {"__stream__": ref_str(obj) if obj.is_indirect else "direct", "dict": d}
    if is_obj(obj, Array):
        return [to_json(v, deref=deref, pages=pages, _top=False, _seen=_seen) for v in obj]
    if is_obj(obj, Dictionary):
        return {str(k): to_json(v, deref=deref, pages=pages, _top=False, _seen=_seen) for k, v in obj.items()}
    return str(obj)


class DecodeError(ValueError):
    pass


def from_json(value: Any, resolve_ref: Callable[[int, int], Any] | None = None) -> Any:
    """Inverse of :func:`to_json` (streams are not accepted)."""
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    if isinstance(value, str):
        if value.startswith("/"):
            return Name(value)
        if value.startswith("u:"):
            return encode_text(value[2:])
        if value.startswith("b:"):
            hexpart = value[2:]
            if not HEX_RE.match(hexpart):
                raise DecodeError(f"invalid hex string: {value[:40]!r}")
            return String(bytes.fromhex("".join(hexpart.split())))
        m = REF_RE.match(value)
        if m:
            if resolve_ref is None:
                raise DecodeError("indirect references are not allowed here")
            return resolve_ref(int(m.group(1)), int(m.group(2)))
        raise DecodeError(
            f"string {value[:40]!r} needs a type prefix: '/Name', 'u:text', 'b:hex' or 'N G R'"
        )
    if isinstance(value, list):
        return Array([from_json(v, resolve_ref) for v in value])
    if isinstance(value, dict):
        if "__stream__" in value:
            raise DecodeError("streams cannot be created from JSON; reference an existing one with 'N G R'")
        d = Dictionary()
        for k, v in value.items():
            if not k.startswith("/"):
                raise DecodeError(f"dictionary key {k!r} must start with '/'")
            decoded = from_json(v, resolve_ref)
            if decoded is not None:
                d[k] = decoded
        return d
    raise DecodeError(f"unsupported JSON value {value!r}")


# --------------------------------------------------------------------------- display


def stream_summary(s: Stream) -> str:
    filt = s.stream_dict.get("/Filter")
    try:
        n = len(s.read_raw_bytes())
    except Exception:
        n = int(s.stream_dict.get("/Length", 0))
    f = f", {brief(filt)}" if filt is not None else ""
    return f"stream ({n:,} bytes{f})"


def brief(obj: Any, pages: dict | None = None, limit: int = 160, _depth: int = 0) -> str:
    """One-line rendering. ``pages`` maps page objgens to page numbers."""
    out = _brief(obj, pages or {}, _depth)
    return out if len(out) <= limit else out[: limit - 1] + "…"


def _brief(obj: Any, pages: dict, depth: int) -> str:
    if obj is None:
        return "null"
    if isinstance(obj, bool):
        return "true" if obj else "false"
    if isinstance(obj, (int, float, Decimal)):
        return str(obj)
    if is_obj(obj, pikepdf.Object) and obj.is_indirect and depth > 0:
        if obj.objgen in pages:
            return f"→ page {pages[obj.objgen]}"
        typ = obj.get("/Type") if is_obj(obj, Dictionary) else None
        return f"{ref_str(obj)}" + (f" ({typ})" if typ is not None else "")
    if is_obj(obj, Name):
        return str(obj)
    if is_obj(obj, String):
        raw = bytes(obj)
        text = decode_text(raw)
        return text if text is not None else f"<{raw.hex()}>"
    if is_obj(obj, Stream):
        return stream_summary(obj)
    if is_obj(obj, Array):
        items = [_brief(v, pages, depth + 1) for v in list(obj)[:8]]
        return "[" + " ".join(items) + (" …" if len(obj) > 8 else "") + "]"
    if is_obj(obj, Dictionary):
        keys = list(obj.keys())
        parts = [f"{k} {_brief(obj[k], pages, depth + 1)}" for k in keys[:5]]
        return "<< " + " ".join(parts) + (f" … ({len(keys)} keys)" if len(keys) > 5 else "") + " >>"
    return str(obj)


def detail(obj: Any, deref: int = 2, pages: dict | None = None) -> str | None:
    """Pretty JSON for the expandable view, or None if the brief form says it all."""
    if not (is_obj(obj, Array) or is_obj(obj, Dictionary) or is_obj(obj, Stream)):
        return None
    try:
        text = json.dumps(to_json(obj, deref=deref, pages=pages or {}), indent=1, ensure_ascii=False)
    except Exception as e:  # pragma: no cover - defensive
        return f"(could not render: {e})"
    if len(text) <= 120:
        return None
    return text if len(text) <= 20_000 else text[:20_000] + "\n… (truncated)"


# --------------------------------------------------------------------------- comparison


class _Budget:
    def __init__(self, n: int) -> None:
        self.n = n


def canonical(obj: Any, pages: dict | None = None, budget: int = 20_000) -> str:
    """Digest of a value, following references, independent of object numbers."""
    b = _Budget(budget)
    try:
        data = _canon(obj, pages or {}, b, ())
    except RecursionError:
        data = "<too deep>"
    payload = json.dumps(data, sort_keys=True, default=str)
    prefix = "partial:" if b.n <= 0 else ""
    return prefix + hashlib.sha256(payload.encode()).hexdigest()[:32]


def _canon(obj: Any, pages: dict, b: _Budget, stack: tuple) -> Any:
    b.n -= 1
    if b.n <= 0:
        return "<budget>"
    if obj is None or isinstance(obj, (bool, int)):
        return obj
    if isinstance(obj, (Decimal, float)):
        return str(Decimal(str(obj)).normalize())
    if is_obj(obj, pikepdf.Object) and obj.is_indirect:
        if obj.objgen in pages:
            return {"page": pages[obj.objgen]}
        if obj.objgen in stack:
            return "<cycle>"
        stack = stack + (obj.objgen,)
    if is_obj(obj, Name):
        return "/" + str(obj)[1:]
    if is_obj(obj, String):
        return "s:" + bytes(obj).hex()
    if is_obj(obj, Stream):
        try:
            data = obj.read_bytes() if int(obj.stream_dict.get("/Length", 0)) < 8_000_000 else obj.read_raw_bytes()
        except Exception:
            data = obj.read_raw_bytes()
        d = {
            str(k): _canon(v, pages, b, stack)
            for k, v in obj.stream_dict.items()
            if k not in ("/Length", "/Filter", "/DecodeParms")
        }
        return {"stream": hashlib.sha256(data).hexdigest(), "dict": d}
    if is_obj(obj, Array):
        return [_canon(v, pages, b, stack) for v in obj]
    if is_obj(obj, Dictionary):
        return {
            str(k): _canon(v, pages, b, stack)
            for k, v in obj.items()
            if k not in ("/Parent", "/P")  # back-pointers are regenerated per file
        }
    return str(obj)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
