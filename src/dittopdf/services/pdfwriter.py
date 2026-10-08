"""A PDF writer that keeps the original's object numbers, bytes and style.

qpdf (and so pikepdf) renumbers every object and re-serializes it in its own
style (spaced, keys sorted) when it writes a file. dittopdf writes the output
itself instead, using the number map from :mod:`numbering` and the original's
bytes from :mod:`rawobjects`:

* an object identical to the original object with the same number is copied
  **byte for byte** from the original file (stream data included);
* a changed object is re-emitted in the **original's style** (compact or spaced
  delimiters, line endings, separators around ``obj``/``stream``), keeping the
  original's dictionary key order and token text (``612`` vs ``612.0``, literal
  vs hex strings) for every part that did not change;
* objects with no original counterpart (the second PDF's content) are written
  in the original's style too, with the second file's key order;
* objects go in the original's physical order, object streams keep the
  original's membership (an unchanged object stream is copied verbatim), and
  the trailer and cross-reference data follow the original's format;
* encryption uses the standard security handler. When the output reuses the
  original's encryption, its ``/Encrypt`` dictionary and file key are reused,
  which also allows verbatim copies of encrypted objects.

:func:`verify` reopens the written file with qpdf and compares every object
with the in-memory document, so the caller can fall back to qpdf's writer.
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
from dittopdf.services.rawobjects import DELIM, WS, Node, RawFile, RawObject, Style, zlib_level


class WriteError(Exception):
    pass


# ----------------------------------------------------------------------------- encryption


class Encryptor:
    """Standard security handler encryption of strings and streams (ISO 32000-2, 7.6.2)."""

    def __init__(self, key: bytes, R: int, stream_method: str, string_method: str, encrypt_metadata: bool,
                 key_id: str = "new"):
        self.key, self.R = key, R
        self.stream_method, self.string_method = stream_method, string_method
        self.encrypt_metadata = encrypt_metadata
        self.key_id = key_id  # identifies the key, so ciphertext can be reused when keys match

    def _object_key(self, num: int, gen: int, aes: bool) -> bytes:
        if self.R >= 5:
            return self.key
        h = hashlib.md5(self.key + num.to_bytes(4, "little")[:3] + gen.to_bytes(2, "little")
                        + (b"sAlT" if aes else b"")).digest()
        return h[: min(len(self.key) + 5, 16)]

    def _crypt(self, method: str, data: bytes, num: int, gen: int, decrypt: bool) -> bytes:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        if method in ("none", "unknown"):
            return data
        if method == "rc4":
            from cryptography.hazmat.decrepit.ciphers.algorithms import ARC4

            c = Cipher(ARC4(self._object_key(num, gen, False)), mode=None)
            op = c.decryptor() if decrypt else c.encryptor()
            return op.update(data) + op.finalize()
        key = self._object_key(num, gen, True)
        if decrypt:
            if len(data) < 32 or len(data) % 16:
                return b"" if len(data) <= 16 else data
            op = Cipher(algorithms.AES(key), modes.CBC(data[:16])).decryptor()
            plain = op.update(data[16:]) + op.finalize()
            pad = plain[-1]
            return plain[:-pad] if 1 <= pad <= 16 and plain.endswith(bytes([pad]) * pad) else plain
        pad = 16 - len(data) % 16
        iv = os.urandom(16)
        op = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        return iv + op.update(data + bytes([pad]) * pad) + op.finalize()

    def string(self, data: bytes, num: int, gen: int) -> bytes:
        return self._crypt(self.string_method, data, num, gen, False)

    def stream(self, data: bytes, num: int, gen: int) -> bytes:
        return self._crypt(self.stream_method, data, num, gen, False)

    def decrypt(self, kind: str, data: bytes, num: int, gen: int) -> bytes:
        method = self.string_method if kind == "string" else self.stream_method
        return self._crypt(method, data, num, gen, True)


@dataclass
class EncryptionSetup:
    encryptor: Encryptor
    dict_bytes: bytes                # the /Encrypt dictionary as written (unencrypted)
    id: list[bytes]
    raw: RawObject | None = None     # the original's /Encrypt object, when reused verbatim


def encryptor_for(pdf: Pdf, key_id: str) -> Encryptor:
    """An Encryptor using the key of an opened encrypted PDF."""
    info = pdf.encryption
    R = int(info.R)
    if R <= 3:  # V1/V2 have no crypt filters: RC4 is implied (pikepdf reports "none")
        stream_m = string_m = "rc4"
    else:
        stream_m, string_m = _method(info.stream_method), _method(info.string_method)
    meta = pdf.trailer.Encrypt.get("/EncryptMetadata", True)
    return Encryptor(bytes(info.encryption_key), R, stream_m, string_m, bool(meta), key_id)


def prepare_encryption(dst: Pdf, encryption: Any, password: str) -> EncryptionSetup:
    """Let qpdf derive a new encryption dictionary and file key for ``dst``'s /ID."""
    buf = io.BytesIO()
    dst.save(buf, encryption=encryption, fix_metadata_version=False)
    with Pdf.open(io.BytesIO(buf.getvalue()), password=password) as tmp:
        enc = encryptor_for(tmp, "new")
        dict_bytes = tmp.trailer.Encrypt.unparse(resolved=True)
        ident = [bytes(x) for x in tmp.trailer.ID]
    return EncryptionSetup(enc, dict_bytes, ident)


def reuse_encryption(src: Pdf, raw_src: RawFile) -> EncryptionSetup | None:
    """Reuse the original's /Encrypt dictionary and file key (same passwords as the original)."""
    encdict = src.trailer.get("/Encrypt")
    if encdict is None or not is_obj(src.trailer.get("/ID"), Array):
        return None
    raw = raw_src.get(encdict.objgen[0]) if encdict.is_indirect else None
    return EncryptionSetup(encryptor_for(src, "orig"), encdict.unparse(resolved=True),
                           [bytes(x) for x in src.trailer.ID], raw)


def _method(m: Any) -> str:
    return str(m).rsplit(".", 1)[-1].lower()


# ----------------------------------------------------------------------------- emitter


class RawBytes(bytes):
    """Pre-serialized value for :meth:`Emitter.dict_items`."""


def _num(v: Any) -> bytes:
    if isinstance(v, bool):
        return b"true" if v else b"false"
    if isinstance(v, int):
        return b"%d" % v
    d = v if isinstance(v, Decimal) else Decimal(repr(v))
    return format(d, "f").encode()


def _same_number_token(raw_value: Any, v: Any) -> bool:
    """True if the raw token can stand for ``v`` exactly (612 is not 612.0, 0.50 is not 0.5)."""
    if isinstance(raw_value, int) != isinstance(v, int):
        return False
    if isinstance(v, int):
        return raw_value == v
    return str(raw_value) == str(v if isinstance(v, Decimal) else Decimal(repr(v)))


def _literal(data: bytes) -> bytes:
    out = bytearray(b"(")
    for b in data:
        if b in (0x28, 0x29, 0x5C):
            out += b"\\" + bytes([b])
        elif b == 0x0D:
            out += b"\\r"
        else:
            out.append(b)
    return bytes(out + b")")


@dataclass
class Ctx:
    """How strings are stored where a value is written or read.

    ``None`` means plaintext; otherwise (key id, num, gen). Raw bytes can be reused
    only when the contexts are equal.
    """
    out: tuple | None
    raw: tuple | None
    raw_verbatim: bool          # raw node comes from the original (its formatting may be reused)
    signature: bool = False


class Emitter:
    def __init__(self, mapping: dict[ObjGen, ObjGen], style: Style, enc: Encryptor | None):
        self.map = mapping
        self.style = style
        self.enc = enc

    # -- joining tokens in the original's style ------------------------------------------

    def join(self, parts: list[bytes]) -> bytes:
        if not self.style.compact:
            return b" ".join(p for p in parts if p)
        out = bytearray()
        for p in parts:
            if not p:
                continue
            if out and out[-1] not in WS + DELIM and p[0] not in WS + DELIM:
                out += b" "
            out += p
        return bytes(out)

    def assemble(self, open_: bytes, close: bytes, pieces: list[tuple[bytes | None, bytes]],
                 close_ws: bytes | None) -> bytes:
        """Join (whitespace, token) pieces. ``None`` whitespace means "as the style requires"."""
        out = bytearray(open_)
        for ws, tok in pieces:
            if ws is not None:
                out += ws
            elif self.style.compact:
                if out[-1:] and out[-1] not in WS + DELIM and tok[:1] and tok[0] not in WS + DELIM:
                    out += b" "
            elif out[-len(open_):] != open_ or open_ == b"<<":
                out += b" "
            out += tok
        if close_ws is not None:
            out += close_ws
        elif not self.style.compact and open_ == b"<<":
            out += b" "
        return bytes(out + close)

    def wrap_dict(self, parts: list[bytes]) -> bytes:
        if self.style.compact:
            return b"<<" + self.join(parts) + b">>"
        return b"<< " + self.join(parts) + b" >>" if parts else b"<< >>"

    def wrap_array(self, parts: list[bytes]) -> bytes:
        return b"[" + self.join(parts) + b"]"

    # -- values ---------------------------------------------------------------------------

    def ref(self, o: Any) -> bytes:
        out = self.map.get(o.objgen)
        return b"null" if out is None else b"%d %d R" % out

    def emit(self, v: Any, raw: Node | None, ctx: Ctx, top: bool = False) -> tuple[bytes, bool]:
        """Serialize ``v``. Returns (bytes, same_as_raw); same_as_raw means ``raw.raw`` was used."""
        verbatim = raw is not None and ctx.raw_verbatim
        if v is None or isinstance(v, (bool, int, float, Decimal)):
            if raw is not None:
                same = (raw.kind == "null" and v is None) or (raw.kind == "bool" and isinstance(v, bool)
                                                            and raw.value == v) or \
                       (raw.kind == "number" and not isinstance(v, bool) and v is not None
                        and _same_number_token(raw.value, v))
                if same:
                    return raw.raw, verbatim
            return (b"null" if v is None else _num(v)), False
        if isinstance(v, RawBytes):
            return bytes(v), False
        if not is_obj(v, pikepdf.Object):
            raise WriteError(f"cannot serialize {type(v).__name__}")
        if v.is_indirect and not top:
            out = self.map.get(v.objgen)
            if raw is not None and raw.kind == "ref" and out is not None and raw.value == out:
                return raw.raw, verbatim
            return self.ref(v), False
        if is_obj(v, Name):
            if raw is not None and raw.kind == "name" and raw.value == str(v):
                return raw.raw, verbatim
            return v.unparse(), False
        if is_obj(v, String):
            data = bytes(v)
            plain = ctx.signature  # signature /Contents are never encrypted
            if raw is not None and raw.kind == "string" and raw.value == data and \
                    (ctx.out == ctx.raw or plain):
                return raw.raw, verbatim
            hexform = raw.hex if raw is not None and raw.kind == "string" else False
            if self.enc is not None and ctx.out is not None and not plain:
                data = self.enc.string(data, ctx.out[1], ctx.out[2])
                hexform = True
            return (b"<" + data.hex().encode() + b">") if hexform else _literal(data), False
        if is_obj(v, Array):
            items = list(v)
            raw_items = raw.items if raw is not None and raw.kind == "array" else []
            parts, all_same = [], raw is not None and raw.kind == "array" and len(raw_items) == len(items)
            for i, item in enumerate(items):
                b, same = self.emit(item, raw_items[i] if i < len(raw_items) else None, ctx)
                parts.append(b)
                all_same = all_same and same
            if all_same and verbatim:
                return raw.raw, True
            if verbatim:  # keep the original's spacing for the items it already had
                pieces = [(_ws_for(raw_items[i], b) if i < len(raw_items) else None, b) for i, b in enumerate(parts)]
                return self.assemble(b"[", b"]", pieces, raw.value if isinstance(raw.value, bytes) else None), False
            return self.wrap_array(parts), False
        if is_obj(v, Dictionary) and not is_obj(v, Stream):
            sig = v.get("/Type") in (Name.Sig, Name.DocTimeStamp)
            return self.dict_items(list(v.items()), raw, Ctx(ctx.out, ctx.raw, ctx.raw_verbatim, sig))
        raise WriteError(f"cannot serialize {v!r}")

    def dict_items(self, items: list[tuple[str, Any]], raw: Node | None, ctx: Ctx) -> tuple[bytes, bool]:
        values = dict(items)
        raw_d = raw if raw is not None and raw.kind == "dict" else None
        order = [k for k in (raw_d.keys() if raw_d else []) if k in values]
        order += [k for k, _ in items if k not in order]
        parts, all_same = [], raw_d is not None and set(raw_d.keys()) == set(values)
        pieces: list[tuple[bytes | None, bytes]] = []
        for k in order:
            rk = None
            if raw_d is not None:
                for key_node, val_node in raw_d.items:
                    if key_node.value == k:
                        rk = (key_node, val_node)
                        break
            key_bytes = rk[0].buf[rk[0].start:rk[0].end] if rk is not None else Name(k).unparse()
            sub = Ctx(ctx.out, ctx.raw, ctx.raw_verbatim, ctx.signature and k == "/Contents")
            b, same = self.emit(values[k], rk[1] if rk else None, sub)
            parts += [key_bytes, b]
            if rk is not None:
                pieces += [(rk[0].ws, key_bytes), (_ws_for(rk[1], b), b)]
            else:
                pieces += [(None, key_bytes), (None if self.style.compact else b" ", b)]
            all_same = all_same and same
        if all_same and raw_d is not None and ctx.raw_verbatim:
            return raw_d.raw, True
        if raw_d is not None and ctx.raw_verbatim:  # keep the original's spacing where it applies
            close_ws = raw_d.value if isinstance(raw_d.value, bytes) else None
            return self.assemble(b"<<", b">>", pieces, close_ws), False
        return self.wrap_dict(parts), False


def _ws_for(raw: Node, new: bytes) -> bytes | None:
    """The original whitespace before ``raw``, if it still fits a token like ``new``."""
    old = raw.raw[:1]
    if not old or not new:
        return None
    if (old[0] in WS + DELIM) == (new[0] in WS + DELIM):
        return raw.ws
    return None


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
    verbatim: list[int] = field(default_factory=list)      # byte-identical to the original object
    restyled: list[int] = field(default_factory=list)      # re-emitted using the original object's bytes
    fresh: list[int] = field(default_factory=list)         # no original counterpart
    objstm_verbatim: list[int] = field(default_factory=list)
    length_objects: list[int] = field(default_factory=list)


@dataclass
class Sources:
    """Where the raw bytes of each output object can come from."""
    original: RawFile | None = None
    second: RawFile | None = None
    from_original: set[ObjGen] = field(default_factory=set)   # dst objects whose number is an original object's
    original_encrypted: bool = False
    second_encrypted: bool = False


def write(dst: Pdf, nb: Numbering, out: Path, header: bytes, layout: Layout, *, ident: list[bytes] | None,
          encryption: EncryptionSetup | None, sources: Sources | None = None, style: Style | None = None) -> Written:
    sources = sources or Sources()
    style = style or Style()
    enc = encryption.encryptor if encryption else None
    em = Emitter(nb.map, style, enc)
    objects = reachable(dst)
    by_num: dict[int, Any] = {}
    for o in objects:
        num = nb.map[o.objgen][0]
        if num in by_num:
            raise WriteError(f"object number {num} assigned twice")
        by_num[num] = o

    md = dst.Root.get("/Metadata")
    clear_metadata = md.objgen if enc is not None and not enc.encrypt_metadata and is_obj(md, Stream) else None
    written = Written(0, {}, None, None, set())

    def raw_for(o: Any) -> tuple[RawObject | None, bool, tuple | None]:
        """(raw object, from the original?, string context of the raw bytes)."""
        num, gen = nb.map[o.objgen]
        if o.objgen in sources.from_original and sources.original is not None:
            r = sources.original.get(num)
            if r is not None and r.gen == gen:
                rctx = ("orig", r.num, r.gen) if sources.original_encrypted and r.in_objstm is None else None
                return r, True, rctx
        if sources.second is not None and not sources.second_encrypted:
            r = sources.second.get(o.objgen[0])
            if r is not None and r.gen == o.objgen[1]:
                return r, False, None
        return None, False, None

    def out_ctx(num: int, gen: int, compressed: bool) -> tuple | None:
        if enc is None or compressed:
            return None
        return (enc.key_id, num, gen)

    # Object streams.
    groups: dict[int, list[int]] = {}
    for num, o in by_num.items():
        k = layout.objstm_of.get(num)
        if k is not None and not is_obj(o, Stream) and nb.map[o.objgen][1] == 0 and k not in by_num:
            groups.setdefault(k, []).append(num)
    compressed = {num for members in groups.values() for num in members}

    # Physical order: the original's, then the second file's, then the rest.
    rank: dict[int, tuple] = {}
    if sources.original is not None:
        for i, n in enumerate(sources.original.physical_order()):
            rank.setdefault(n, (0, i))
    second_rank = {n: i for i, n in enumerate(sources.second.physical_order())} if sources.second else {}

    # Indirect /Length objects of original streams: written where the original had them.
    length_candidates: set[int] = set()
    if sources.original is not None:
        for num, o in by_num.items():
            if is_obj(o, Stream) and o.objgen in sources.from_original:
                r = sources.original.get(num)
                rl = r.value.get("/Length") if r is not None and r.value.kind == "dict" else None
                if rl is not None and rl.kind == "ref" and rl.value[0] not in by_num:
                    length_candidates.add(rl.value[0])

    def order_key(num: int) -> tuple:
        o = by_num.get(num)
        if o is not None and o.objgen in sources.from_original and num in rank:
            return rank[num]
        if (num in groups or num in length_candidates) and num in rank:
            return rank[num]
        if o is not None and o.objgen[0] in second_rank:
            return (1, second_rank[o.objgen[0]])
        return (2, num)

    bodies: dict[int, bytes] = {}       # compressed members' serialized bodies
    member_same: dict[int, bool] = {}
    for num in compressed:
        o = by_num[num]
        r, from_orig, rctx = raw_for(o)
        b, same = em.emit(o, r.value if r is not None else None,
                          Ctx(None, rctx, from_orig), top=True)
        bodies[num] = b
        member_same[num] = same and from_orig and r is not None and r.in_objstm is not None

    buf = bytearray(header)
    xref: dict[int, tuple[int, int, int]] = {}
    used_numbers = set(by_num) | set(groups)
    top_level = sorted([n for n in by_num if n not in compressed] + list(groups)
                       + [n for n in length_candidates if n not in groups], key=order_key)
    lengths_written: set[int] = set()

    def write_length(num: int) -> None:
        r = sources.original.get(num)
        xref[num] = (1, len(buf), r.gen)
        buf.extend(r.raw)
        if r.raw[-1:] not in (b"\n", b"\r", b" "):
            buf.extend(b"\n")
        lengths_written.add(num)

    for num in top_level:
        if num in length_candidates and num not in by_num:
            if num in written.length_objects:  # its stream (written earlier) was copied verbatim
                write_length(num)
            continue
        if num in groups:
            _write_objstm(num, groups[num], bodies, member_same, buf, xref, em, style, enc, sources, written,
                          used_numbers)
            continue
        o = by_num[num]
        gen = nb.map[o.objgen][1]
        r, from_orig, rctx = raw_for(o)
        octx = out_ctx(num, gen, False)
        start = len(buf)
        if is_obj(o, Stream):
            chunk, verbatim = _stream_object(o, num, gen, r, from_orig, rctx, octx, em, style, enc, layout,
                                             clear_metadata, sources, used_numbers, written)
        else:
            body, same = em.emit(o, r.value if r is not None else None, Ctx(octx, rctx, from_orig), top=True)
            verbatim = same and from_orig and r is not None and r.in_objstm is None and (r.num, r.gen) == (num, gen)
            if verbatim:
                chunk = r.raw
            else:
                chunk = (b"%d %d obj" % (num, gen) + style.after_obj + body + style.before_endobj + b"endobj"
                         + style.after_endobj)
        buf += chunk
        if chunk[-1:] not in (b"\n", b"\r", b" "):
            buf += style.after_endobj or b"\n"  # a verbatim span may end right after "endobj"
        xref[num] = (1, start, gen)
        (written.verbatim if verbatim else written.restyled if r is not None and from_orig
         else written.fresh).append(num)

    # Indirect /Length objects that verbatim streams refer to.
    for num in written.length_objects:
        if num not in lengths_written:  # its stream came after it in the original
            write_length(num)

    if encryption is not None:
        en = layout.encrypt_num
        if en is None or en in xref:
            en = max(xref) + 1
        xref[en] = (1, len(buf), 0)
        r = encryption.raw
        if r is not None and r.num == en and r.in_objstm is None:
            buf += r.raw
        else:
            buf += (b"%d 0 obj" % en + style.after_obj + encryption.dict_bytes + style.before_endobj + b"endobj"
                    + style.after_endobj)
        written.encrypt_num = en

    # Trailer and cross-reference data.
    tr = dst.trailer
    items: list[tuple[str, Any]] = []
    for key in ("/Root", "/Info"):
        if key in tr and is_obj(tr[key], pikepdf.Object) and tr[key].is_indirect and tr[key].objgen in nb.map:
            items.append((key, tr[key]))
    if ident:
        items.append(("/ID", Array([String(x) for x in ident])))
    if written.encrypt_num is not None:
        items.append(("/Encrypt", RawBytes(b"%d 0 R" % written.encrypt_num)))
    for k, v in tr.items():
        if k not in TRAILER_SKIP:
            items.append((k, v))
    raw_trailer = sources.original.trailer() if sources.original is not None else None
    tctx = Ctx(None, None, raw_trailer is not None)
    eol = style.eol

    xnum = None
    if layout.xref_stream or groups:
        xnum = layout.xref_num if layout.xref_num and layout.xref_num not in xref else max(xref) + 1
        size = max(max(xref) + 1, xnum + 1, layout.size_min)
        xref[xnum] = (1, len(buf), 0)
        w2 = max(1, (max(v[1] for v in xref.values()).bit_length() + 7) // 8)
        w3 = max(1, (max(max(v[2] for v in xref.values()), 65535).bit_length() + 7) // 8)
        enc_x = _xref_encoding(sources, raw_trailer)
        if enc_x["w"] is not None and enc_x["w"][1] >= w2:
            ow3 = enc_x["w"][2]
            if ow3 >= w3:
                w2, w3 = enc_x["w"][1], ow3
            elif ow3 == 0 and all(v[2] == 0 for n, v in xref.items() if n != xnum):
                w2, w3 = enc_x["w"][1], 0  # no generation column: every value defaults to 0
        # The xref stream always lists itself: some originals leave their own entry out, but readers
        # treat that as an error, so it is not reproduced.
        count = size
        rows = []
        for i in range(count):
            t, f2, f3 = xref.get(i, (0, 0, 65535 if i == 0 else 0))
            rows.append(bytes([t]) + f2.to_bytes(w2, "big") + (f3.to_bytes(w3, "big") if w3 else b""))
        plain = b"".join(rows)
        parms = None
        if enc_x["predictor"]:
            plain = _png_up(rows)
            parms = Dictionary(Columns=1 + w2 + w3, Predictor=enc_x["predictor"])
        level = enc_x["level"] if enc_x["level"] is not None else style.zlib_level
        data = zlib.compress(plain, level if level is not None else 6)
        index = [("/Index", Array([0, count]))] if enc_x["index"] is not None else []
        xitems = [("/Type", Name.XRef), ("/Size", size)] + index + [("/W", Array([1, w2, w3]))] + items + \
                 [("/Filter", Name.FlateDecode)] + ([("/DecodeParms", parms)] if parms is not None else []) + \
                 [("/Length", len(data))]
        d, _ = em.dict_items(xitems, raw_trailer, tctx)
        buf += (b"%d 0 obj" % xnum + style.after_obj + d + style.before_stream + b"stream" + style.stream_eol
                + data + style.before_endstream + b"endstream" + style.before_endobj + b"endobj"
                + style.after_endobj)
        start = xref[xnum][1]
    else:
        size = max(max(xref) + 1, layout.size_min)
        start = len(buf)
        buf += b"xref" + eol + b"0 %d" % size + eol
        tail = style.xref_entry_eol
        for i in range(size):
            if i in xref:
                buf += b"%010d %05d n" % (xref[i][1], xref[i][2]) + tail
            else:
                buf += b"0000000000 %05d f" % (65535 if i == 0 else 0) + tail
        d, _ = em.dict_items([("/Size", size)] + items, raw_trailer, tctx)
        buf += b"trailer" + eol + d + eol
    buf += b"startxref" + eol + b"%d" % start + eol + b"%%EOF" + eol
    out.write_bytes(bytes(buf))
    written.size, written.xref_num = size, xnum
    written.objstms = {k: sorted(v) for k, v in groups.items()}
    return written


def _xref_encoding(sources: Sources, raw_trailer: Node | None) -> dict:
    """How the original encoded its newest cross-reference stream (widths, predictor, zlib level)."""
    out: dict[str, Any] = {"w": None, "predictor": None, "level": None, "index": None}
    orig = sources.original
    nums = getattr(orig, "xref_stream_nums", None) if orig is not None else None
    if not nums or raw_trailer is None or raw_trailer.kind != "dict":
        return out
    w = raw_trailer.get("/W")
    if w is not None and w.kind == "array" and len(w.items) == 3 and w.items[0].value == 1:
        out["w"] = [n.value for n in w.items]
    idx = raw_trailer.get("/Index")
    if idx is not None and idx.kind == "array" and len(idx.items) == 2:
        out["index"] = [n.value for n in idx.items]
    parms = raw_trailer.get("/DecodeParms")
    pred = parms.get("/Predictor") if parms is not None and parms.kind == "dict" else None
    if pred is not None and pred.value in (10, 11, 12, 13, 14, 15):
        out["predictor"] = pred.value
    objs = getattr(orig, "xref_stream_objs", None)
    raw = objs[0] if objs else orig.get(nums[0])
    if raw is not None and raw.data is not None and not sources.original_encrypted:
        try:
            out["level"] = zlib_level(raw.data, zlib.decompress(raw.data))
        except zlib.error:
            pass
    return out


def _png_up(rows: list[bytes]) -> bytes:
    """PNG 'Up' prediction (the filter PDF writers use for xref streams)."""
    out, prev = bytearray(), bytes(len(rows[0])) if rows else b""
    for row in rows:
        out.append(2)
        out += bytes((b - p) & 0xFF for b, p in zip(row, prev))
        prev = row
    return bytes(out)


def _deflate(data: bytes, style: Style) -> bytes:
    return zlib.compress(data, style.zlib_level if style.zlib_level is not None else 6)


def _stream_object(o: Stream, num: int, gen: int, r: RawObject | None, from_orig: bool, rctx: tuple | None,
                   octx: tuple | None, em: Emitter, style: Style, enc: Encryptor | None, layout: Layout,
                   clear_metadata: ObjGen | None, sources: Sources, used_numbers: set[int],
                   written: Written) -> tuple[bytes, bool]:
    sd = o.stream_dict
    data = o.read_raw_bytes()
    items = [(k, v) for k, v in sd.items() if k != "/Length"]
    if layout.compress and "/Filter" not in sd:
        data = _deflate(o.read_bytes(), style)
        items = [(k, v) for k, v in items if k != "/DecodeParms"] + [("/Filter", Name.FlateDecode)]
        written.compressed_streams.add(o.objgen)
    filters = sd.get("/Filter")
    identity = filters == Name.Crypt or (is_obj(filters, Array) and Name.Crypt in list(filters))
    encrypt_data = enc is not None and not identity and o.objgen != clear_metadata
    data_ctx = octx if encrypt_data else None

    # Is the original's raw stream data usable as-is?
    raw_plain = sources.original.stream_plain_raw(r) if (r is not None and from_orig) else None
    raw_dict = r.value if r is not None and r.value.kind == "dict" else None
    data_same = raw_plain is not None and r.reliable and raw_plain == data
    data_raw_ctx = rctx if (rctx is not None and sources.original_encrypted) else None
    out_data = data
    if data_same and data_ctx == data_raw_ctx:
        out_data = r.data
    elif encrypt_data:
        out_data = enc.stream(data, num, gen)

    # /Length keeps its original form when it still holds; an indirect length object is kept too.
    length_item: Any = len(out_data)
    if raw_dict is not None:
        rl = raw_dict.get("/Length")
        if rl is not None and rl.kind == "ref" and from_orig and sources.original is not None:
            lo = sources.original.get(rl.value[0])
            if lo is not None and lo.value.kind == "number" and lo.value.value == len(out_data) \
                    and rl.value[0] not in used_numbers and lo.in_objstm is None:
                length_item = RawBytes(rl.raw)
                used_numbers.add(rl.value[0])
                written.length_objects.append(rl.value[0])
    pos = [k for k, _ in sd.items()].index("/Length") if "/Length" in sd else len(items)
    items.insert(min(pos, len(items)), ("/Length", length_item))
    d, dict_same = em.dict_items(items, raw_dict, Ctx(octx, rctx, from_orig))
    if isinstance(length_item, RawBytes) and raw_dict is not None:
        # dict_items cannot compare a pre-serialized value; the length object proves it.
        dict_same = _dict_same_except_length(em, items, raw_dict, octx, rctx, from_orig)
    verbatim = (dict_same and data_same and data_ctx == data_raw_ctx and from_orig and r is not None
                and r.in_objstm is None and (r.num, r.gen) == (num, gen))
    if verbatim:
        return r.raw, True
    return (b"%d %d obj" % (num, gen) + style.after_obj + d + style.before_stream + b"stream" + style.stream_eol
            + out_data + style.before_endstream + b"endstream" + style.before_endobj + b"endobj"
            + style.after_endobj), False


def _dict_same_except_length(em: Emitter, items: list, raw_dict: Node, octx: Any, rctx: Any, from_orig: bool) -> bool:
    if set(raw_dict.keys()) != {k for k, _ in items}:
        return False
    for k, v in items:
        if k == "/Length":
            continue
        _, same = em.emit(v, raw_dict.get(k), Ctx(octx, rctx, from_orig))
        if not same:
            return False
    return True


def _length_object(raw: RawObject, sources: Sources, used_numbers: set[int]) -> int | None | bool:
    """For a verbatim copy of ``raw``: the number of its indirect /Length object to copy too.

    Returns None if /Length is direct, the object number if it can be copied, False if it cannot.
    """
    rl = raw.value.get("/Length") if raw.value.kind == "dict" else None
    if rl is None or rl.kind != "ref":
        return None
    lo = sources.original.get(rl.value[0]) if sources.original is not None else None
    if lo is None or lo.value.kind != "number" or lo.in_objstm is not None or \
            lo.value.value != (raw.data_end or 0) - (raw.data_start or 0) or \
            rl.value[0] in used_numbers:
        return False
    return rl.value[0]


def _write_objstm(k: int, members: list[int], bodies: dict[int, bytes], member_same: dict[int, bool],
                  buf: bytearray, xref: dict, em: Emitter, style: Style, enc: Encryptor | None, sources: Sources,
                  written: Written, used_numbers: set[int]) -> None:
    orig = sources.original
    orig_members = orig.objstm_members(k) if orig is not None else []
    order = [n for n in orig_members if n in members] + sorted(n for n in members if n not in orig_members)
    raw = orig.get(k) if orig is not None and orig_members else None
    # Original ObjStm ciphertext is reusable only under the same key (or with no encryption at all).
    if sources.original_encrypted:
        same_key = enc is not None and enc.key_id == "orig"
    else:
        same_key = enc is None
    length_obj = _length_object(raw, sources, used_numbers) if raw is not None else None
    if (raw is not None and order == orig_members and all(member_same.get(n) for n in order) and same_key
            and raw.in_objstm is None and raw.num == k and raw.reliable and length_obj is not False):
        if length_obj is not None:
            used_numbers.add(length_obj)
            written.length_objects.append(length_obj)
        xref[k] = (1, len(buf), raw.gen)
        buf += raw.raw
        for i, n in enumerate(order):
            xref[n] = (2, k, i)
        written.objstm_verbatim.append(k)
        return
    sep = b"\n"
    plain = orig.objstm_plain(k) if raw is not None else None
    if plain is not None and len(orig_members) > 1:
        first_body = orig.get(orig_members[0])
        if first_body is not None:
            gap_end = first_body.end
            nxt = plain[gap_end:gap_end + 8]
            sep = nxt[:len(nxt) - len(nxt.lstrip(WS))] or b" "
    offsets, chunks, pos = [], [], 0
    for i, n in enumerate(order):
        offsets.append(b"%d %d" % (n, pos))
        chunks.append(bodies[n])
        pos += len(bodies[n]) + len(sep)
        xref[n] = (2, k, i)
    head = b" ".join(offsets) + b" "
    data = _deflate(head + sep.join(chunks) + sep, style)
    if enc is not None:
        data = enc.stream(data, k, 0)
    d, _ = em.dict_items([("/Type", Name.ObjStm), ("/N", len(order)), ("/First", len(head)),
                          ("/Filter", Name.FlateDecode), ("/Length", len(data))],
                         raw.value if raw is not None else None, Ctx(None, None, raw is not None))
    xref[k] = (1, len(buf), 0)
    buf += (b"%d 0 obj" % k + style.after_obj + d + style.before_stream + b"stream" + style.stream_eol + data
            + style.before_endstream + b"endstream" + style.before_endobj + b"endobj" + style.after_endobj)


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
