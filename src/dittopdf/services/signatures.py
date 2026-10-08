"""Digital signature discovery and certificate inspection (read-only).

Signatures are never copied: a signature covers the exact bytes of the file
that was signed (``/ByteRange``), so moving it to a different document, or
rewriting the signed document, makes it invalid. This module only reports.
"""

from __future__ import annotations

from typing import Any, Iterator

from pikepdf import Dictionary, Name, String

from dittopdf.services.pdfobj import is_obj

SIG_KEYS = ["/Type", "/Filter", "/SubFilter", "/ByteRange", "/Contents", "/M", "/Name",
            "/Location", "/Reason", "/ContactInfo", "/Cert", "/Prop_Build", "/Reference"]


def iter_signature_fields(pdf: Any) -> Iterator[tuple[str, Any, Any]]:
    """Yield (fully qualified field name, field dict, signature dict or None)."""
    acro = pdf.Root.get("/AcroForm")
    if not is_obj(acro, Dictionary):
        return
    seen: set = set()

    def walk(field: Any, parent_name: str, inherited_ft: Any, depth: int) -> Iterator:
        if depth > 40 or not is_obj(field, Dictionary):
            return
        key = field.objgen if field.is_indirect else id(field)
        if key in seen:
            return
        seen.add(key)
        t = field.get("/T")
        name = f"{parent_name}.{t}" if parent_name and t is not None else (str(t) if t is not None else parent_name)
        ft = field.get("/FT", inherited_ft)
        kids = field.get("/Kids")
        if ft == Name.Sig and "/V" in field:
            yield name or "(unnamed)", field, field.get("/V")
        elif ft == Name.Sig and not kids:
            yield name or "(unnamed)", field, None
        if kids is not None:
            for kid in kids:
                yield from walk(kid, name, ft, depth + 1)

    for f in acro.get("/Fields", []) or []:
        yield from walk(f, "", None, 0)


def der_slice(data: bytes) -> bytes:
    """Trim the zero padding that follows a DER object in /Contents."""
    if len(data) < 2 or data[0] != 0x30:
        return data.rstrip(b"\x00")
    n = data[1]
    if n < 0x80:
        return data[: 2 + n]
    k = n & 0x7F
    length = int.from_bytes(data[2: 2 + k], "big")
    return data[: 2 + k + length]


def certificates(sig: Any) -> tuple[list[dict], str | None]:
    """Extract and describe the signer certificates. Returns (certs, error)."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.serialization import pkcs7
    except ImportError:  # pragma: no cover
        return [], "the 'cryptography' package is not installed"
    sub = str(sig.get("/SubFilter", ""))
    certs = []
    try:
        if sub == "/adbe.x509.rsa_sha1" and "/Cert" in sig:
            raw = sig["/Cert"]
            blobs = [bytes(raw)] if is_obj(raw, String) else [bytes(c) for c in raw]
            certs = [x509.load_der_x509_certificate(b) for b in blobs]
        elif "/Contents" in sig:
            data = der_slice(bytes(sig["/Contents"]))
            certs = pkcs7.load_der_pkcs7_certificates(data)
    except Exception as e:
        return [], f"could not parse certificates: {e}"
    out = []
    for c in certs:
        key = c.public_key()
        key_desc = type(key).__name__.replace("_", "").replace("PublicKey", "")
        size = getattr(key, "key_size", None)
        try:
            sig_hash = c.signature_hash_algorithm.name if c.signature_hash_algorithm else "?"
        except Exception:
            sig_hash = "?"
        out.append({
            "Subject": c.subject.rfc4514_string(),
            "Issuer": c.issuer.rfc4514_string(),
            "Valid from": c.not_valid_before_utc.isoformat(),
            "Valid until": c.not_valid_after_utc.isoformat(),
            "Serial number": format(c.serial_number, "x"),
            "SHA-256 fingerprint": c.fingerprint(hashes.SHA256()).hex(),
            "SHA-1 fingerprint": c.fingerprint(hashes.SHA1()).hex(),
            "Public key algorithm": key_desc + (f" ({size} bits)" if size else ""),
            "Signature algorithm": f"{c.signature_algorithm_oid._name} (hash {sig_hash})",
        })
    return out, None


def byte_range_coverage(sig: Any, file_size: int) -> str | None:
    br = sig.get("/ByteRange")
    if br is None or len(br) != 4:
        return None
    a, b, c, d = (int(x) for x in br)
    end = c + d
    if a == 0 and end == file_size:
        return "Covers the whole file (except the signature value)"
    return (f"Covers bytes 0–{end:,} of {file_size:,}: the file was changed after signing "
            "(incremental updates follow the signed revision)")
