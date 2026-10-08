"""Core logic for copying metadata between PDFs.

A PDF carries metadata in two places:

* the document information dictionary (``/Info`` in the trailer) holding keys
  such as ``/Title``, ``/Author``, ``/Producer``, ``/CreationDate``;
* an XMP metadata stream (``/Metadata`` in the document catalog), an XML packet
  that newer tools (and PDF 2.0) treat as authoritative.

Both are copied verbatim so the target ends up with exactly the source's
metadata, rather than a re-generated approximation.
"""

from __future__ import annotations

from os import PathLike
from pathlib import Path

import pikepdf
from pikepdf import Dictionary, Name, Pdf

StrPath = str | PathLike[str]


def _copy_info(src: Pdf, dst: Pdf, merge: bool) -> None:
    if not merge:
        # Drop the target's Info dictionary entirely; a fresh one is created below.
        if Name.Info in dst.trailer:
            del dst.trailer[Name.Info]
    if Name.Info not in src.trailer:
        return
    info = dst.docinfo  # creates an empty indirect /Info if missing
    for key, value in src.docinfo.items():
        # Info values are almost always direct strings/dates; only indirect
        # objects need to be imported from the foreign PDF.
        info[key] = dst.copy_foreign(value) if value.is_indirect else value


def _copy_xmp(src: Pdf, dst: Pdf, merge: bool) -> None:
    src_xmp = src.Root.get(Name.Metadata)
    if src_xmp is None or not isinstance(src_xmp, pikepdf.Stream):
        if not merge and Name.Metadata in dst.Root:
            del dst.Root[Name.Metadata]
        return
    # Copy the raw (decoded) XML bytes into a fresh stream instead of using
    # copy_foreign, so no unrelated objects from the source are dragged along.
    stream = pikepdf.Stream(dst, src_xmp.read_bytes())
    stream[Name.Type] = Name.Metadata
    stream[Name.Subtype] = Name.XML
    dst.Root[Name.Metadata] = dst.make_indirect(stream)


def copy_metadata(
    source: StrPath,
    target: StrPath,
    output: StrPath | None = None,
    *,
    info: bool = True,
    xmp: bool = True,
    merge: bool = False,
    source_password: str = "",
    target_password: str = "",
) -> Path:
    """Copy metadata from ``source`` into ``target`` and write the result.

    Args:
        source: PDF to take metadata from.
        target: PDF whose content is kept and whose metadata is replaced.
        output: Where to write the result. ``None`` overwrites ``target``.
        info: Copy the document information dictionary.
        xmp: Copy the XMP metadata stream.
        merge: Keep target keys/XMP that the source does not have instead of
            clearing the target's metadata first.
        source_password / target_password: Passwords for encrypted inputs.

    Returns:
        Path of the written file.
    """
    target_path = Path(target)
    out_path = Path(output) if output is not None else target_path
    in_place = out_path.resolve() == target_path.resolve()

    with (
        Pdf.open(source, password=source_password) as src,
        Pdf.open(target_path, password=target_password, allow_overwriting_input=in_place) as dst,
    ):
        if info:
            _copy_info(src, dst, merge)
        if xmp:
            _copy_xmp(src, dst, merge)
        # Keep the PDF version at least as high as the source's, since copied
        # metadata may rely on it. Encryption of the target is preserved.
        dst.save(
            out_path,
            min_version=src.pdf_version,
            encryption=dst.is_encrypted,  # True preserves existing encryption
        )
    return out_path


def read_metadata(path: StrPath, password: str = "") -> dict[str, object]:
    """Return a PDF's Info dictionary (as strings) and raw XMP XML, for inspection."""
    with Pdf.open(path, password=password) as pdf:
        docinfo = {str(k): str(v) for k, v in pdf.docinfo.items()} if Name.Info in pdf.trailer else {}
        xmp_obj = pdf.Root.get(Name.Metadata)
        xmp = xmp_obj.read_bytes().decode("utf-8", "replace") if isinstance(xmp_obj, pikepdf.Stream) else None
    return {"info": docinfo, "xmp": xmp}
