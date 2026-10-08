"""A small PDF writer that keeps chosen object numbers.

qpdf (and so pikepdf) renumbers every object when it writes a file. To give the
output the original's object numbers, dittopdf serializes the in-memory
document itself, using the map from :mod:`numbering`:

* objects are written as ``N G obj`` with references renumbered through the map;
* objects can be placed in object streams with chosen numbers, and the
  cross-reference data is a classic table or a stream (with a chosen number);
* the header block, the full ``/ID`` and extra trailer keys are written exactly;
* encryption uses the standard security handler: qpdf produces the encryption
  dictionary and file key (by writing an encrypted copy), and each string and
  stream is encrypted here with the key for its own object number.

:func:`verify` reopens the written file with qpdf and compares every object
with the in-memory original, so the caller can fall back to qpdf's writer if
anything is off.
"""

from __future__ import annotations

import hashlib
import io
import os
import zlib
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import pikepdf
from pikepdf import Array, Dictionary, Name, Pdf, Stream, String

from dittopdf.services.numbering import TRAILER_SKIP, Numbering, ObjGen, reachable
from dittopdf.services.pdfobj import is_obj


class WriteError(Exception):
    pass


# ----------------------------------------------------------------------------- encryption


class Encryptor:
    """Standard security handler encryption of strings and streams (ISO 32000-2, 7.6.2)."""

    def __init__(self, key: bytes, R: int, stream_method: str, string_method: str, encrypt_metadata: bool):
        self.key, self.R = key, R
        self.stream_method, self.string_method = stream_method, string_method
        self.encrypt_metadata = encrypt_metadata

    def _object_key(self, num: int, gen: int, aes: bool) -> bytes:
        if self.R >= 5:
            return self.key
        h = hashlib.md5(self.key + num.to_bytes(4, "little")[:3] + gen.to_bytes(2, "little")
                        + (b"sAlT" if aes else b"")).digest()
        return h[: min(len(self.key) + 5, 16)]

    def _encrypt(self, method: str, data: bytes, num: int, gen: int) -> bytes:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        if method in ("none", "unknown"):
            return data
        if method == "rc4":
            from cryptography.hazmat.decrepit.ciphers.algorithms import ARC4

            enc = Cipher(ARC4(self._object_key(num, gen, False)), mode=None).encryptor()
            return enc.update(data) + enc.finalize()
        key = self._object_key(num, gen, True)
        pad = 16 - len(data) % 16
        iv = os.urandom(16)
        enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        return iv + enc.update(data + bytes([pad]) * pad) + enc.finalize()

    def string(self, data: bytes, num: int, gen: int) -> bytes:
        return self._encrypt(self.string_method, data, num, gen)

    def stream(self, data: bytes, num: int, gen: int) -> bytes:
        return self._encrypt(self.stream_method, data, num, gen)


@dataclass
class EncryptionSetup:
    encryptor: Encryptor
    dict_bytes: bytes
    id: list[bytes]


def prepare_encryption(dst: Pdf, encryption: pikepdf.Encryption, password: str) -> EncryptionSetup:
    """Let qpdf derive the encryption dictionary and file key for ``dst``'s /ID."""
    buf = io.BytesIO()
    dst.save(buf, encryption=encryption, fix_metadata_version=False)
    with Pdf.open(io.BytesIO(buf.getvalue()), password=password) as tmp:
        info = tmp.encryption
        encdict = tmp.trailer.Encrypt
        ser = Serializer({}, None)
        dict_bytes = ser.value(encdict, None, top=True)
        meta = encdict.get("/EncryptMetadata", True)
        R = int(info.R)
        if R <= 3:  # V1/V2 have no crypt filters: RC4 is implied (pikepdf reports "none")
            stream_m = string_m = "rc4"
        else:
            stream_m, string_m = _method(info.stream_method), _method(info.string_method)
        enc = Encryptor(bytes(info.encryption_key), R, stream_m, string_m, bool(meta))
        ident = [bytes(x) for x in tmp.trailer.ID]
    return EncryptionSetup(enc, dict_bytes, ident)


def _method(m: Any) -> str:
    return str(m).rsplit(".", 1)[-1].lower()


# ----------------------------------------------------------------------------- serializer


def _num(v: Any) -> bytes:
    if isinstance(v, bool):
        return b"true" if v else b"false"
    if isinstance(v, int):
        return b"%d" % v
    d = v if isinstance(v, Decimal) else Decimal(repr(v))
    return format(d, "f").encode()


class Serializer:
    def __init__(self, mapping: dict[ObjGen, ObjGen], encryptor: Encryptor | None):
        self.map = mapping
        self.enc = encryptor

    def ref(self, o: Any) -> bytes:
        out = self.map.get(o.objgen)
        return b"null" if out is None else b"%d %d R" % out

    def value(self, o: Any, ctx: ObjGen | None, *, top: bool = False, plain_strings: bool = False) -> bytes:
        """Serialize ``o``. ``ctx`` is the containing object's (num, gen) for string encryption."""
        if o is None:
            return b"null"
        if isinstance(o, (bool, int, float, Decimal)):
            return _num(o)
        if not is_obj(o, pikepdf.Object):
            raise WriteError(f"cannot serialize {type(o).__name__}")
        if o.is_indirect and not top:
            return self.ref(o)
        if is_obj(o, Name):
            return o.unparse()
        if is_obj(o, String):
            if self.enc is not None and ctx is not None and not plain_strings:
                return b"<" + self.enc.string(bytes(o), *ctx).hex().encode() + b">"
            return o.unparse()
        if is_obj(o, Array):
            return b"[" + b" ".join(self.value(v, ctx, plain_strings=plain_strings) for v in o) + b"]"
        if is_obj(o, Stream):
            raise WriteError("streams must be written as top-level objects")
        if is_obj(o, Dictionary):
            return self.dict_bytes(o.items(), ctx, signature=o.get("/Type") in (Name.Sig, Name.DocTimeStamp),
                                   plain_strings=plain_strings)
        raise WriteError(f"cannot serialize {o!r}")

    def dict_bytes(self, items: Any, ctx: ObjGen | None, *, signature: bool = False, plain_strings: bool = False,
                   extra: list[bytes] | None = None) -> bytes:
        parts = []
        for k, v in items:
            # Signature values are not encrypted (ISO 32000-2, 7.6.2).
            plain = plain_strings or (signature and k == "/Contents")
            parts.append(Name(k).unparse() + b" " + self.value(v, ctx, plain_strings=plain))
        parts.extend(extra or [])
        return b"<< " + b" ".join(parts) + b" >>"


# ----------------------------------------------------------------------------- writer


@dataclass
class Layout:
    """Where each output object goes. All numbers are output object numbers."""
    objstm_of: dict[int, int] = field(default_factory=dict)  # object -> object stream holding it
    xref_stream: bool = False
    xref_num: int | None = None
    encrypt_num: int | None = None
    size_min: int = 0
    compress: bool = False


@dataclass
class Written:
    size: int
    objstms: dict[int, list[int]]
    xref_num: int | None
    encrypt_num: int | None
    compressed_streams: set[ObjGen]


def write(dst: Pdf, nb: Numbering, out: Path, header: bytes, layout: Layout, *,
          ident: list[bytes] | None, encryption: EncryptionSetup | None) -> Written:
    enc = encryption.encryptor if encryption else None
    ser = Serializer(nb.map, enc)
    # With /EncryptMetadata false only the document's own XMP stream stays in the clear.
    md = dst.Root.get("/Metadata")
    clear_metadata = md.objgen if enc is not None and not enc.encrypt_metadata and is_obj(md, Stream) else None
    objects = reachable(dst)
    by_num: dict[int, Any] = {}
    for o in objects:
        num = nb.map[o.objgen][0]
        if num in by_num:
            raise WriteError(f"object number {num} assigned twice")
        by_num[num] = o

    buf = bytearray(header)
    xref: dict[int, tuple[int, int, int]] = {}  # num -> (type, field2, field3)
    groups: dict[int, list[int]] = {}
    for num, o in by_num.items():
        k = layout.objstm_of.get(num)
        if k is not None and not is_obj(o, Stream) and nb.map[o.objgen][1] == 0 and k not in by_num:
            groups.setdefault(k, []).append(num)
    compressed = {num for members in groups.values() for num in members}
    recompressed: set[ObjGen] = set()

    for num in sorted(by_num):
        if num in compressed:
            continue
        o = by_num[num]
        gen = nb.map[o.objgen][1]
        xref[num] = (1, len(buf), gen)
        buf += b"%d %d obj\n" % (num, gen)
        if is_obj(o, Stream):
            body, changed = _stream(o, ser, (num, gen), enc if o.objgen != clear_metadata else None,
                                    layout.compress)
            if changed:
                recompressed.add(o.objgen)
            buf += body
        else:
            buf += ser.value(o, (num, gen), top=True)
        buf += b"\nendobj\n"

    for k in sorted(groups):
        members = sorted(groups[k])
        bodies, offsets, pos = [], [], 0
        for i, num in enumerate(members):
            body = ser.value(by_num[num], None, top=True, plain_strings=True)  # the ObjStm itself is encrypted
            offsets.append(b"%d %d" % (num, pos))
            bodies.append(body)
            pos += len(body) + 1
            xref[num] = (2, k, i)
        head = b" ".join(offsets) + b"\n"
        data = zlib.compress(head + b"\n".join(bodies) + b"\n", 9)
        if enc is not None:
            data = enc.stream(data, k, 0)
        xref[k] = (1, len(buf), 0)
        buf += (b"%d 0 obj\n<< /Type /ObjStm /N %d /First %d /Filter /FlateDecode /Length %d >>\nstream\n"
                % (k, len(members), len(head), len(data)) + data + b"\nendstream\nendobj\n")

    encrypt_ref = b""
    if encryption is not None:
        en = layout.encrypt_num
        if en is None or en in xref:
            en = max(xref) + 1
        xref[en] = (1, len(buf), 0)
        buf += b"%d 0 obj\n" % en + encryption.dict_bytes + b"\nendobj\n"
        encrypt_ref = b"/Encrypt %d 0 R" % en
    else:
        en = None

    tr = dst.trailer
    tparts = []
    for key in ("/Root", "/Info"):
        if key in tr and is_obj(tr[key], pikepdf.Object) and tr[key].is_indirect:
            tparts.append(key.encode() + b" " + ser.ref(tr[key]))
    if ident:
        tparts.append(b"/ID [" + b"".join(b"<" + x.hex().encode() + b">" for x in ident) + b"]")
    if encrypt_ref:
        tparts.append(encrypt_ref)
    for k, v in tr.items():
        if k not in TRAILER_SKIP:
            tparts.append(Name(k).unparse() + b" " + ser.value(v, None, plain_strings=True))

    xnum = None
    if layout.xref_stream or groups:
        xnum = layout.xref_num if layout.xref_num and layout.xref_num not in xref else max(xref) + 1
        size = max(max(xref) + 1, xnum + 1, layout.size_min)
        xref[xnum] = (1, len(buf), 0)
        w2 = max(1, (max(v[1] for v in xref.values()).bit_length() + 7) // 8)
        w3 = max(1, (max(max(v[2] for v in xref.values()), 65535).bit_length() + 7) // 8)
        rows = []
        for i in range(size):
            t, f2, f3 = xref.get(i, (0, 0, 65535 if i == 0 else 0))
            rows.append(bytes([t]) + f2.to_bytes(w2, "big") + f3.to_bytes(w3, "big"))
        data = zlib.compress(b"".join(rows), 9)
        buf += (b"%d 0 obj\n<< /Type /XRef /Size %d /W [ 1 %d %d ] " % (xnum, size, w2, w3) + b" ".join(tparts)
                + b" /Filter /FlateDecode /Length %d >>\nstream\n" % len(data) + data + b"\nendstream\nendobj\n")
        start = xref[xnum][1]
    else:
        size = max(max(xref) + 1, layout.size_min)
        start = len(buf)
        buf += b"xref\n0 %d\n" % size
        for i in range(size):
            if i in xref:
                buf += b"%010d %05d n\r\n" % (xref[i][1], xref[i][2])
            else:
                buf += b"0000000000 %05d f\r\n" % (65535 if i == 0 else 0)
        buf += b"trailer\n<< /Size %d " % size + b" ".join(tparts) + b" >>\n"
    buf += b"startxref\n%d\n%%%%EOF\n" % start
    out.write_bytes(bytes(buf))
    return Written(size, {k: sorted(v) for k, v in groups.items()}, xnum, en, recompressed)


def _stream(o: Stream, ser: Serializer, ctx: ObjGen, enc: Encryptor | None, compress: bool) -> tuple[bytes, bool]:
    sd = o.stream_dict
    data = o.read_raw_bytes()
    items = [(k, v) for k, v in sd.items() if k != "/Length"]
    changed = False
    if compress and "/Filter" not in sd:
        data = zlib.compress(o.read_bytes(), 9)
        items = [(k, v) for k, v in items if k != "/DecodeParms"] + [("/Filter", Name.FlateDecode)]
        changed = True
    filters = sd.get("/Filter")
    crypt_identity = filters == Name.Crypt or (is_obj(filters, Array) and Name.Crypt in list(filters))
    if enc is not None and not crypt_identity:
        data = enc.stream(data, *ctx)
    head = ser.dict_bytes(items, ctx, extra=[b"/Length %d" % len(data)])
    return head + b"\nstream\n" + data + b"\nendstream", changed


# ----------------------------------------------------------------------------- verification


def verify(dst: Pdf, nb: Numbering, path: Path, password: str, written: Written) -> str | None:
    """Reopen ``path`` and compare every object with ``dst``. Returns a problem or None."""
    try:
        with Pdf.open(path, password=password) as out:
            for o in reachable(dst):
                b = out.get_object(nb.map[o.objgen])
                problem = _same(o, b, nb.map, written.compressed_streams, top=True)
                if problem:
                    n, g = nb.map[o.objgen]
                    return f"object {n} {g}: {problem}"
            if len(out.pages) != len(dst.pages):
                return "page count differs"
            warnings = out.get_warnings()
            if warnings:
                return f"qpdf needed to repair the file ({warnings[0]})"
    except Exception as e:
        return f"{type(e).__name__}: {e}"
    return None


def _same(a: Any, b: Any, mapping: dict, recompressed: set, top: bool = False) -> str | None:
    if a is None or isinstance(a, (bool, int, float, Decimal)):
        if isinstance(a, bool) or isinstance(b, bool):
            return None if a is b else "boolean differs"
        if a is None:
            return None if b is None else "null expected"
        try:
            return None if Decimal(str(a)) == Decimal(str(b)) else f"number {a} != {b}"
        except Exception:
            return f"number {a!r} != {b!r}"
    if a.is_indirect and not top:
        want = mapping.get(a.objgen)
        if want is None:
            return None if b is None else "dangling reference expected to be null"
        return None if is_obj(b, pikepdf.Object) and b.is_indirect and b.objgen == want else "reference differs"
    if is_obj(a, Name):
        return None if is_obj(b, Name) and str(a) == str(b) else "name differs"
    if is_obj(a, String):
        return None if is_obj(b, String) and bytes(a) == bytes(b) else "string differs"
    if is_obj(a, Stream):
        if not is_obj(b, Stream):
            return "stream expected"
        skip = {"/Length"} | ({"/Filter", "/DecodeParms"} if a.objgen in recompressed else set())
        ka = {k for k in a.stream_dict.keys() if k not in skip}
        kb = {k for k in b.stream_dict.keys() if k not in skip}
        if ka != kb:
            return f"stream keys differ ({sorted(ka ^ kb)})"
        for k in ka:
            p = _same(a.stream_dict[k], b.stream_dict[k], mapping, recompressed)
            if p:
                return f"{k}: {p}"
        if a.objgen in recompressed:
            return None if a.read_bytes() == b.read_bytes() else "stream data differs"
        return None if a.read_raw_bytes() == b.read_raw_bytes() else "stream data differs"
    if is_obj(a, Array):
        if not is_obj(b, Array) or len(a) != len(b):
            return "array differs"
        for x, y in zip(a, b):
            p = _same(x, y, mapping, recompressed)
            if p:
                return p
        return None
    if is_obj(a, Dictionary):
        if not is_obj(b, Dictionary) or set(a.keys()) != set(b.keys()):
            return "dictionary keys differ"
        for k in a.keys():
            p = _same(a[k], b[k], mapping, recompressed)
            if p:
                return f"{k}: {p}"
        return None
    return None
