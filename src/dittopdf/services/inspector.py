"""Full inspection of one PDF into a list of entries (see :mod:`model`).

Every section of the inspection is isolated: a malformed structure in one part
of a file produces an "unable to inspect" entry for that section and the rest
of the inspection continues.
"""

from __future__ import annotations

import hashlib
import io
import re
from collections import Counter
from pathlib import Path
from typing import Any, Callable

import pikepdf
from pikepdf import Array, Dictionary, Name, Pdf, Stream, String

from dittopdf.services import fonts as fontmod
from dittopdf.services import numbering, pdfobj, rawfile, resources, signatures, xmp
from dittopdf.services.model import (
    COPY, DIRECT, KEEP, READONLY, RECONSTRUCT, REGENERATE, REMOVE, UNREPRODUCIBLE,
    entry, info_entry, obj_entry,
)
from dittopdf.services.pdfobj import brief, is_obj

# ----------------------------------------------------------------------------- field tables

INFO_STANDARD = ["/Title", "/Author", "/Subject", "/Keywords", "/Creator", "/Producer",
                 "/CreationDate", "/ModDate", "/Trapped"]
INFO_DATES = {"/CreationDate", "/ModDate", "/SourceModified"}
LEGACY_INFO = {
    "/DocChecksum": "Legacy Acrobat checksum of the document.",
    "/SourceModified": "Legacy: modification date of the source document (Acrobat Web Capture/PDFMaker).",
    "/Company": "Legacy Microsoft Office document property.",
    "/Manager": "Legacy Microsoft Office document property.",
    "/Category": "Legacy Microsoft Office document property.",
    "/GTS_PDFXVersion": "PDF/X identification (Info dictionary form).",
    "/GTS_PDFXConformance": "PDF/X conformance level (Info dictionary form).",
    "/PTEX.Fullbanner": "pdfTeX/LuaTeX build banner.",
    "/AAPL:Keywords": "macOS Quartz keywords array.",
}
CHOICES = {
    "/Trapped": ["/True", "/False", "/Unknown"],
    "/PageLayout": ["/SinglePage", "/OneColumn", "/TwoColumnLeft", "/TwoColumnRight", "/TwoPageLeft", "/TwoPageRight"],
    "/PageMode": ["/UseNone", "/UseOutlines", "/UseThumbs", "/FullScreen", "/UseOC", "/UseAttachments"],
    "/Tabs": ["/R", "/C", "/S", "/A", "/W"],
    "/Direction": ["/L2R", "/R2L"],
    "/NonFullScreenPageMode": ["/UseNone", "/UseOutlines", "/UseThumbs", "/UseOC"],
    "/PrintScaling": ["/None", "/AppDefault"],
    "/Duplex": ["/Simplex", "/DuplexFlipShortEdge", "/DuplexFlipLongEdge"],
}

OPTION, NONCOPY = "option", "noncopy"
# (class, editable, default action, note); see spec_kw().
Spec = tuple[str, bool, str | None, str]
CATALOG: dict[str, Spec] = {
    "/Type": (REGENERATE, False, None, "Always /Catalog; written by the PDF writer."),
    "/Version": (DIRECT, True, None, "Overrides the header version when higher than it."),
    "/Extensions": (DIRECT, True, None, "Developer extension levels (e.g. ADBE)."),
    "/Pages": (READONLY, False, None, "The page tree is the second PDF's content and is kept."),
    "/PageLabels": (RECONSTRUCT, True, None, "Number tree of page labels; applies by page index."),
    "/Names": (RECONSTRUCT, False, None, ""),
    "/Dests": (RECONSTRUCT, True, None, "Named destinations; page references are remapped by page number."),
    "/ViewerPreferences": (DIRECT, True, None, ""),
    "/PageLayout": (DIRECT, True, None, ""),
    "/PageMode": (DIRECT, True, None, ""),
    "/Outlines": (RECONSTRUCT, False, None, "Bookmarks are copied as a new tree; destinations are remapped to the same page numbers."),
    "/Threads": (RECONSTRUCT, False, None, "Article threads; beads are remapped to the same page numbers."),
    "/OpenAction": (RECONSTRUCT, True, None, "Page references are remapped by page number."),
    "/AA": (RECONSTRUCT, True, None, "Document-level additional actions (scripts)."),
    "/URI": (DIRECT, True, None, ""),
    "/AcroForm": (RECONSTRUCT, False, OPTION, "Form fields are bound to widget annotations on pages, so the "
                  "interactive form follows the 'Annotations & forms' output option."),
    "/Metadata": (READONLY, False, None, "Handled in the XMP section."),
    "/StructTreeRoot": (UNREPRODUCIBLE, False, OPTION, "The tag tree points into the page content (marked-content IDs). "
                        "The original's tags do not describe the second PDF's content; see the 'Structure tree' output option."),
    "/MarkInfo": (DIRECT, True, None, ""),
    "/Lang": (DIRECT, True, None, ""),
    "/SpiderInfo": (DIRECT, True, None, "Legacy Web Capture information."),
    "/OutputIntents": (RECONSTRUCT, False, None, "Copied with their embedded ICC profiles."),
    "/PieceInfo": (RECONSTRUCT, False, None, "Application private data (Illustrator, InDesign, …) is copied unchanged."),
    "/OCProperties": (UNREPRODUCIBLE, False, KEEP, "Optional-content groups are referenced from page content; "
                      "the original's groups do not match the second PDF's content unless the content is the same."),
    "/Perms": (UNREPRODUCIBLE, False, NONCOPY, "Permission signatures (DocMDP/UR) refer to signatures that cannot be transferred."),
    "/Legal": (UNREPRODUCIBLE, False, NONCOPY, "Legal attestation tied to a signature."),
    "/Requirements": (DIRECT, True, None, ""),
    "/Collection": (RECONSTRUCT, False, None, "Portfolio (collection) definition."),
    "/NeedsRendering": (DIRECT, True, None, "XFA rendering flag."),
    "/DSS": (UNREPRODUCIBLE, False, KEEP, "Validation data (certificates, OCSP, CRL) for signatures; "
             "meaningless without the signatures it supports."),
    "/AF": (RECONSTRUCT, False, None, "Associated files; embedded files are copied."),
    "/DPartRoot": (RECONSTRUCT, False, None, "Document-part hierarchy; page ranges are remapped by page number."),
}
SPLIT = {
    "/ViewerPreferences": ("Viewer preferences", ["/HideToolbar", "/HideMenubar", "/HideWindowUI", "/FitWindow",
                           "/CenterWindow", "/DisplayDocTitle", "/NonFullScreenPageMode", "/Direction", "/ViewArea",
                           "/ViewClip", "/PrintArea", "/PrintClip", "/PrintScaling", "/Duplex",
                           "/PickTrayByPDFSize", "/PrintPageRange", "/NumCopies", "/Enforce"], DIRECT),
    "/MarkInfo": ("Mark info (/MarkInfo)", ["/Marked", "/UserProperties", "/Suspects"], DIRECT),
    "/Names": ("Name dictionary (/Names)", ["/Dests", "/AP", "/JavaScript", "/Pages", "/Templates", "/IDS",
               "/URLS", "/EmbeddedFiles", "/AlternatePresentations", "/Renditions"], RECONSTRUCT),
}
NAMES_NOTES = {
    "/Dests": "Named destinations; page references are remapped by page number.",
    "/EmbeddedFiles": "Embedded files are copied with their file streams and parameters.",
    "/JavaScript": "Document-level JavaScript.",
    "/AP": "Named appearance streams.",
    "/Pages": "Named pages refer to the original's pages; they are remapped by page number.",
    "/Templates": "Template pages are copied as invisible pages are, if referenced.",
}

TRAILER: dict[str, Spec] = {
    "/Size": (REGENERATE, False, None, "Number of objects in the output; computed by the writer."),
    "/Root": (REGENERATE, False, None, "Reference to the catalog; the catalog's contents are handled in the Catalog section."),
    "/Info": (REGENERATE, False, None, "Reference to the Info dictionary; its contents are handled in Document Info."),
    "/ID": (RECONSTRUCT, True, None, "File identifier. See the 'File identifier' output option for how it is reproduced."),
    "/Encrypt": (REGENERATE, False, None, "Encryption dictionary; see the 'Encryption' output option."),
    "/Prev": (REGENERATE, False, None, "Offset of the previous cross-reference section; a rewritten file has a single revision."),
    "/XRefStm": (REGENERATE, False, None, "Hybrid-file pointer; regenerated by the writer."),
    "/DocChecksum": (DIRECT, True, None, "Legacy checksum key; copied into the output trailer."),
}
XREF_KEYS = ["/Type", "/Index", "/W", "/Size", "/Root", "/Info", "/ID", "/Prev", "/Encrypt", "/Length",
             "/Filter", "/DecodeParms"]

PAGE: dict[str, Spec] = {
    "/Type": (REGENERATE, False, None, ""),
    "/Parent": (REGENERATE, False, None, "Position in the page tree; the second PDF's page tree is kept."),
    "/LastModified": (DIRECT, True, None, ""),
    "/MediaBox": (DIRECT, True, None, "Changing page boxes changes the visible page geometry."),
    "/CropBox": (DIRECT, True, None, "Changing page boxes changes the visible page geometry."),
    "/BleedBox": (DIRECT, True, None, ""),
    "/TrimBox": (DIRECT, True, None, ""),
    "/ArtBox": (DIRECT, True, None, ""),
    "/BoxColorInfo": (DIRECT, True, None, ""),
    "/Rotate": (DIRECT, True, None, "Changing rotation changes how the page is displayed."),
    "/Group": (READONLY, False, None, "Transparency group of the page's own content; kept from the second PDF."),
    "/Thumb": (UNREPRODUCIBLE, False, KEEP, "A thumbnail image of the original page's content; it would not depict "
               "the second PDF's page. Copy only if the content is the same."),
    "/B": (RECONSTRUCT, False, None, "Article beads; copied together with /Threads."),
    "/Dur": (DIRECT, True, None, ""),
    "/Trans": (DIRECT, True, None, ""),
    "/PresSteps": (RECONSTRUCT, False, None, ""),
    "/Annots": (READONLY, False, None, "Annotations are handled in the Annotations section and the "
                "'Annotations & forms' output option."),
    "/AA": (RECONSTRUCT, True, None, "Page open/close actions."),
    "/Metadata": (RECONSTRUCT, False, None, "Page-level XMP stream."),
    "/PieceInfo": (RECONSTRUCT, False, None, "Application private data."),
    "/StructParents": (UNREPRODUCIBLE, False, OPTION, "Key into the structure tree's parent tree; follows the "
                       "'Structure tree' output option."),
    "/ID": (DIRECT, True, None, "Web Capture digital identifier."),
    "/PZ": (DIRECT, True, None, ""),
    "/SeparationInfo": (RECONSTRUCT, False, None, ""),
    "/Tabs": (DIRECT, True, None, ""),
    "/TemplateInstantiated": (DIRECT, True, None, ""),
    "/UserUnit": (DIRECT, True, None, "Changes the physical size of the page."),
    "/VP": (RECONSTRUCT, True, None, "Viewports with measurement / geospatial data."),
    "/AF": (RECONSTRUCT, False, None, ""),
    "/OutputIntents": (RECONSTRUCT, False, None, ""),
    "/DPart": (RECONSTRUCT, False, None, "Document part; copied with /DPartRoot."),
}
PAGE_CONTENT_KEYS = {"/Contents", "/Resources"}
INHERITABLE = {"/MediaBox", "/CropBox", "/Rotate"}

ANNOT_COMMON = ["/Type", "/Subtype", "/Rect", "/Contents", "/P", "/NM", "/M", "/F", "/AP", "/AS", "/Border",
                "/C", "/StructParent", "/OC", "/AF", "/ca", "/CA", "/BM", "/Lang"]
ANNOT_MARKUP = ["/T", "/Popup", "/RC", "/CreationDate", "/Subj", "/IRT", "/RT", "/IT", "/ExData"]
MARKUP_SUBTYPES = {"/Text", "/FreeText", "/Line", "/Square", "/Circle", "/Polygon", "/PolyLine", "/Highlight",
                   "/Underline", "/Squiggly", "/StrikeOut", "/Stamp", "/Caret", "/Ink", "/FileAttachment",
                   "/Sound", "/Redact", "/Projection"}
ANNOT_DIRECT = {"/Contents", "/NM", "/M", "/F", "/C", "/CA", "/ca", "/BM", "/Lang", "/T", "/RC",
                "/CreationDate", "/Subj", "/RT", "/IT", "/Border"}
ANNOT_RECON = {"/Popup", "/IRT", "/OC", "/AF", "/ExData"}
ANNOT_REGEN = {"/Type", "/P"}

IMAGE_KEYS = ["/Width", "/Height", "/ColorSpace", "/BitsPerComponent", "/Filter", "/DecodeParms", "/Intent",
              "/ImageMask", "/SMask", "/Mask", "/Decode", "/Interpolate", "/Metadata", "/OPI", "/ID",
              "/Measure", "/PtData", "/SMaskInData", "/Alternates", "/StructParent", "/Name"]
FD_KEYS = ["/FontName", "/FontFamily", "/FontStretch", "/FontWeight", "/Flags", "/FontBBox", "/ItalicAngle",
           "/Ascent", "/Descent", "/Leading", "/CapHeight", "/XHeight", "/StemV", "/StemH", "/AvgWidth",
           "/MaxWidth", "/MissingWidth", "/CharSet", "/CIDSet", "/Style", "/Lang", "/FD"]

SECTIONS = ["Document Info", "XMP", "Catalog", "Trailer", "Structure", "Pages", "Annotations", "Signatures",
            "Fonts", "Images", "Embedded files", "PieceInfo", "Output intents", "Measurement & geospatial",
            "Document parts", "Legacy metadata", "Filesystem & transport"]


class InspectError(Exception):
    """The file cannot be opened as a PDF at all."""


# ----------------------------------------------------------------------------- context


class Ctx:
    def __init__(self, pdf: Pdf, data: bytes, max_pages: int) -> None:
        self.pdf = pdf
        self.data = data
        self.max_pages = max_pages
        self.entries: list[dict] = []
        self.pages: dict[tuple, int] = {}
        self.annots: dict[tuple, tuple[int, int]] = {}
        self.res: resources.Resources | None = None
        try:
            for i, p in enumerate(pdf.pages, 1):
                self.pages[p.obj.objgen] = i
                for j, a in enumerate(p.obj.get("/Annots", []) or [], 1):
                    if is_obj(a, Dictionary) and a.is_indirect:
                        self.annots[a.objgen] = (i, j)
        except Exception:
            pass

    def add(self, e: dict) -> dict:
        self.entries.append(e)
        return e

    def obj(self, id: str, path: list[str], label: str, value: Any, **kw: Any) -> dict:
        return self.add(obj_entry(id, path, label, value, pages=self.pages, **kw))

    def info(self, id: str, path: list[str], label: str, value: Any, **kw: Any) -> dict:
        return self.add(info_entry(id, path, label, value, **kw))

    def section(self, name: str, fn: Callable[["Ctx"], None]) -> None:
        try:
            fn(self)
        except Exception as e:
            self.add(entry(f"{name}:__error__", [name, "Inspection error"], "Unable to inspect this section",
                           error=f"{type(e).__name__}: {e}"))

    def ref_display(self, obj: Any) -> str:
        if is_obj(obj, pikepdf.Object) and obj.is_indirect:
            if obj.objgen in self.annots:
                p, j = self.annots[obj.objgen]
                return f"→ annotation {j} on page {p}"
            if obj.objgen in self.pages:
                return f"→ page {self.pages[obj.objgen]}"
        return brief(obj, self.pages)


def spec_kw(spec: Spec) -> dict[str, Any]:
    """Entry keyword arguments for a table spec.

    The default slot is None (derive from the class), KEEP (opt-in copy),
    OPTION (controlled by an output option, no per-field actions) or NONCOPY
    (may be kept or removed, never copied).
    """
    cls, editable, default, note = spec
    kw: dict[str, Any] = {"cls": cls, "editable": editable, "note": note}
    if default == OPTION:
        kw["actions"], kw["default"] = [], KEEP
    elif default == NONCOPY:
        kw["actions"], kw["default"] = [KEEP, REMOVE], KEEP
    elif default == KEEP:
        kw["actions"], kw["default"] = [KEEP, COPY, REMOVE], KEEP
    return kw


# ----------------------------------------------------------------------------- entry point


def inspect_pdf(path: str | Path, password: str = "", *, filename: str = "", fs: dict | None = None,
                max_pages: int = 200) -> dict[str, Any]:
    """Inspect a PDF. Raises :class:`pikepdf.PasswordError` or :class:`InspectError`."""
    data = Path(path).read_bytes()
    try:
        pdf = Pdf.open(io.BytesIO(data), password=password)
    except pikepdf.PasswordError:
        raise
    except Exception as e:
        msg = re.sub(r"stream <[^>]*>:?\s*", "", str(e))
        raise InspectError(f"Not a readable PDF: {msg}") from e
    with pdf:
        warnings = _warnings(pdf)
        ctx = Ctx(pdf, data, max_pages)
        ctx.warnings = warnings  # type: ignore[attr-defined]
        raw = rawfile.analyse(data)
        ctx.raw = raw  # type: ignore[attr-defined]
        try:
            ctx.res = resources.collect(pdf, max_pages)
        except Exception:
            ctx.res = resources.Resources(errors=["resource scan failed"])
        builders = {
            "Document Info": _info, "XMP": _xmp, "Catalog": _catalog, "Trailer": _trailer,
            "Structure": _structure, "Pages": _pages, "Annotations": _annotations,
            "Signatures": _signatures, "Fonts": _fonts, "Images": _images,
            "Embedded files": _embedded, "PieceInfo": _pieceinfo, "Output intents": _output_intents,
            "Measurement & geospatial": _measure, "Document parts": _dparts,
            "Legacy metadata": _legacy,
        }
        for name, fn in builders.items():
            ctx.section(name, fn)
        _filesystem(ctx, filename, fs or {}, len(data))
        title = pdf.docinfo.get("/Title") if "/Info" in pdf.trailer else None
        summary = {
            "filename": filename,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "pages": len(pdf.pages),
            "version": pdf.pdf_version,
            "header_version": raw["header_version"],
            "encrypted": pdf.is_encrypted,
            "linearized": pdf.is_linearized,
            "title": pdfobj.decode_text(bytes(title)) if is_obj(title, String) else None,
            "signed": any(e["id"].endswith(":/ByteRange") and e["present"] for e in ctx.entries
                          if e["id"].startswith("sig:")),
            "warnings": (warnings + _warnings(pdf))[:50],
            "detail_pages": min(len(pdf.pages), max_pages),
            "tagged": "/StructTreeRoot" in pdf.Root,
        }
    return {"summary": summary, "entries": ctx.entries}


# ----------------------------------------------------------------------------- sections


def _warnings(pdf: Pdf) -> list[str]:
    """qpdf's warnings so far (reading them clears qpdf's queue)."""
    return [re.sub(r"^stream <[^>]*>:?\s*", "", str(w)) for w in pdf.get_warnings()]



def _info(ctx: Ctx) -> None:
    pdf = ctx.pdf
    info = pdf.trailer.get("/Info")
    if not is_obj(info, Dictionary):
        info = None
    P = ["Document Info", "Standard fields"]
    for k in INFO_STANDARD:
        kind = "date" if k in INFO_DATES else ("name" if k == "/Trapped" else "text")
        ctx.obj(f"info:{k}", P, k[1:], info.get(k) if info is not None else None, cls=DIRECT, editable=True,
                absent_kind=kind, choices=CHOICES.get(k))
    if info is None:
        return
    for k in sorted(set(info.keys()) - set(INFO_STANDARD)):
        v = info.get(k)
        ctx.obj(f"info:{k}", ["Document Info", "Private / non-standard keys"], k[1:], v, cls=DIRECT,
                editable=True, note=LEGACY_INFO.get(k, "Producer-specific key; copied as-is."))


def _xmp(ctx: Ctx) -> None:
    md = ctx.pdf.Root.get("/Metadata")
    P = ["XMP", "Packet"]
    parsed: dict[str, Any] = {"props": [], "ok": False}
    if is_obj(md, Stream):
        try:
            raw = md.read_bytes()
        except Exception as e:
            ctx.add(entry("xmp:packet", P, "XMP packet", error=f"Unable to decode metadata stream: {e}",
                          cls=DIRECT))
            return
        parsed = xmp.parse(raw)
        text = raw.decode("utf-8", "replace")
        ctx.add(entry("xmp:packet", P, "XMP packet (complete XML)",
                      display=f"{len(raw):,} bytes · {len(parsed['props'])} top-level properties",
                      kind="xml", edit=text, canon="x:" + hashlib.sha256(raw).hexdigest(), cls=DIRECT,
                      editable=True,
                      note="Copied byte-for-byte when unchanged. If edited (here or per property below), "
                           "the packet is re-serialized with all other properties preserved."))
        sd = {k: v for k, v in md.stream_dict.items() if k != "/Length"}
        ctx.info("xmp:stream", P, "Metadata stream dictionary", brief(Dictionary(sd)) if sd else None,
                 note="A new /Type /Metadata /Subtype /XML stream is written.")
        ctx.info("xmp:toolkit", P, "XMP toolkit (x:xmptk)", parsed.get("toolkit"))
        ctx.info("xmp:about", P, "rdf:about", parsed.get("about"))
        ctx.info("xmp:wrapper", P, "xpacket wrapper", parsed.get("wrapper"))
        if parsed.get("error"):
            ctx.add(entry("xmp:parse", P, "Parsed properties", error=parsed["error"]))
    else:
        ctx.add(entry("xmp:packet", P, "XMP packet (complete XML)", present=False, kind="xml", cls=DIRECT,
                      editable=True, note="No /Metadata stream in the catalog."))
    seen = set()
    for p in parsed["props"]:
        seen.add((p["uri"], p["local"]))
        kind = xmp.value_kind(p["value"])
        e = ctx.add(entry(xmp.prop_id(p["uri"], p["local"]), ["XMP", xmp.schema_title(p["prefix"], p["uri"])],
                          f"{p['prefix']}:{p['local']}", display=xmp.display(p["value"]), kind=kind,
                          edit=xmp.edit_value(p["value"]), canon=xmp.canonical(p["value"]), cls=RECONSTRUCT,
                          editable=kind != "xmp-struct",
                          note="" if kind != "xmp-struct" else
                          "Structured value: copy, keep or remove it here; edit its XML in the packet above."))
        e["xmp"] = {"uri": p["uri"], "local": p["local"], "prefix": p["prefix"], "xml": p["xml"],
                    "vtype": {"alt": "L", "bag": "B", "seq": "S"}.get(p["value"]["t"], "T")}
    for prefix in xmp.ALWAYS_SHOWN:
        uri, _ = xmp.NAMESPACES[prefix]
        for local, vtype in xmp.SCHEMA_PROPS.get(prefix, []):
            if (uri, local) in seen:
                continue
            kind = {"T": "xmp-text", "L": "xmp-text", "B": "xmp-list", "S": "xmp-list"}.get(vtype, "xmp-struct")
            e = ctx.add(entry(xmp.prop_id(uri, local), ["XMP", xmp.schema_title(prefix, uri)],
                              f"{prefix}:{local}", present=False, kind=kind, cls=RECONSTRUCT,
                              editable=kind != "xmp-struct"))
            e["xmp"] = {"uri": uri, "local": local, "prefix": prefix, "xml": None, "vtype": vtype}


def _catalog(ctx: Ctx) -> None:
    root = ctx.pdf.Root
    keys = list(CATALOG) + sorted(set(root.keys()) - set(CATALOG))
    for k in keys:
        if k in SPLIT:
            title, subkeys, cls = SPLIT[k]
            sub = root.get(k)
            sub = sub if is_obj(sub, Dictionary) else None
            extra = sorted(set(sub.keys()) - set(subkeys)) if sub is not None else []
            P = ["Catalog", title]
            if k in root and sub is None:
                ctx.add(entry(f"catalog:{k}", P, k, error="Not a dictionary"))
            for sk in subkeys + extra:
                v = sub.get(sk) if sub is not None else None
                note = NAMES_NOTES.get(sk, "") if k == "/Names" else ""
                if k == "/Names" and v is not None:
                    try:
                        note = f"{len(pikepdf.NameTree(v)):,} names. " + note
                    except Exception:
                        pass
                ctx.obj(f"catalog:{k}{sk}", P, f"{k}{sk}", v, cls=cls,
                        editable=cls == DIRECT, choices=CHOICES.get(sk), note=note)
            continue
        spec = CATALOG.get(k, (RECONSTRUCT, True, None, "Non-standard catalog key; copied as-is."))
        v = root.get(k)
        if k == "/Pages":
            ctx.info("catalog:/Pages", ["Catalog", "Catalog entries"], "/Pages",
                     f"{pdfobj.ref_str(v)} ({len(ctx.pdf.pages)} pages)" if v is not None else None,
                     cls=READONLY, note=spec[3])
            continue
        if k == "/Metadata":
            ctx.info("catalog:/Metadata", ["Catalog", "Catalog entries"], "/Metadata",
                     pdfobj.stream_summary(v) if is_obj(v, Stream) else None, cls=READONLY, note=spec[3])
            continue
        if k == "/Type":
            ctx.info("catalog:/Type", ["Catalog", "Catalog entries"], "/Type", str(v) if v is not None else None,
                     cls=REGENERATE, note=spec[3])
            continue
        ctx.obj(f"catalog:{k}", ["Catalog", "Catalog entries"], k, v, choices=CHOICES.get(k), **spec_kw(spec))


def _ref_entry(ctx: Ctx, id: str, P: list[str], label: str, v: Any, note: str) -> None:
    if v is None:
        ctx.add(entry(id, P, label, present=False, cls=REGENERATE, note=note))
        return
    disp = pdfobj.ref_str(v) if is_obj(v, pikepdf.Object) and v.is_indirect else brief(v)
    ctx.add(entry(id, P, label, display=disp, cls=REGENERATE, note=note, canon="ref"))


def _trailer(ctx: Ctx) -> None:
    tr = ctx.pdf.trailer
    is_stream = tr.get("/Type") == Name.XRef
    P = ["Trailer", "Trailer dictionary"]
    for k, spec in TRAILER.items():
        v = tr.get(k)
        if k in ("/Root", "/Info", "/Encrypt"):
            _ref_entry(ctx, f"trailer:{k}", P, k, v, spec[3])
            continue
        if k == "/ID" and is_obj(v, Array):
            e = ctx.obj("trailer:/ID", P, "/ID", v, **spec_kw(spec))
            e["display"] = " ".join(f"<{bytes(x).hex()}>" for x in v)
            if len(v) != 2:
                e["warn"] = f"Non-standard /ID with {len(v)} element(s) (legacy)."
            continue
        ctx.obj(f"trailer:{k}", P, k, v, **spec_kw(spec))
    for k in sorted(set(tr.keys()) - set(TRAILER) - set(XREF_KEYS)):
        ctx.obj(f"trailer:{k}", P, k, tr.get(k), cls=DIRECT, editable=True,
                note="Non-standard trailer key; qpdf writes it into the output trailer.")
    PX = ["Trailer", "Cross-reference stream dictionary"]
    if is_stream:
        for k in XREF_KEYS:
            v = tr.get(k)
            if k in ("/Root", "/Info", "/Encrypt"):
                _ref_entry(ctx, f"xref:{k}", PX, k, v, "Regenerated by the writer.")
            else:
                ctx.obj(f"xref:{k}", PX, k, v, cls=REGENERATE,
                        note="Cross-reference stream fields are produced by the PDF writer.")
    else:
        ctx.info("xref:kind", PX, "Cross-reference stream", "Not used (classic xref table)", cls=REGENERATE)


def _structure(ctx: Ctx) -> None:
    pdf, raw = ctx.pdf, ctx.raw  # type: ignore[attr-defined]
    P = ["Structure", "Versions"]
    ctx.info("structure:header_version", P, "File header version (%PDF-x.y)", raw["header_version"],
             cls=RECONSTRUCT, note="Reproduced by the 'PDF version' output option.")
    cv = pdf.Root.get("/Version")
    ctx.info("structure:catalog_version", P, "Catalog /Version", str(cv) if cv is not None else None,
             cls=DIRECT, note="See Catalog → /Version.")
    ext = f" (extension level {pdf.extension_level})" if pdf.extension_level else ""
    ctx.info("structure:effective_version", P, "Effective PDF version", pdf.pdf_version + ext, cls=RECONSTRUCT)
    if raw["header_offset"]:
        ctx.info("structure:header_offset", P, "Bytes before %PDF header", raw["header_offset"],
                 cls=REGENERATE, warn="Junk before the header.")
    block = bytes.fromhex(raw["header_block"]) if raw.get("header_block") else b""
    e = ctx.info("structure:binary_marker", P, "Header bytes (header line + binary marker)",
                 f"{_escape(block)}\nhex: {block.hex(' ')}" if block else None, cls=RECONSTRUCT,
                 note="Reproduced byte for byte by the 'Header bytes' output option (offsets are corrected).")
    if block:
        e["canon"] = "v:" + block.hex()

    P = ["Structure", "Pages"]
    ctx.info("structure:page_count", P, "Page count", len(pdf.pages), cls=READONLY,
             note="Pages are the second PDF's content and are never added or removed.")
    sizes: Counter = Counter()
    for page in list(pdf.pages)[:5000]:
        try:
            mb = [float(x) for x in page.mediabox]
            rot = int(page.obj.get("/Rotate", 0) or 0)
            sizes[(round(mb[2] - mb[0], 2), round(mb[3] - mb[1], 2), rot % 360)] += 1
        except Exception:
            sizes[("?", "?", 0)] += 1
    ctx.info("structure:page_sizes", P, "Page sizes (MediaBox)",
             "; ".join(_size_text(w, h, r) + f" × {n}" for (w, h, r), n in sizes.most_common()), cls=READONLY,
             note="Per-page boxes are listed (and copyable) in the Pages section.")
    marked = pdf.Root.get("/MarkInfo", {}).get("/Marked") if is_obj(pdf.Root.get("/MarkInfo"), Dictionary) else None
    ctx.info("structure:tagged", P, "Tagged PDF (accessibility)",
             f"{'Yes' if '/StructTreeRoot' in pdf.Root else 'No'} structure tree; /MarkInfo /Marked "
             f"{'true' if marked else 'false/absent'}", cls=UNREPRODUCIBLE,
             note="Tags describe the page content; see the 'Structure tree' output option.")

    P = ["Structure", "Encryption"]
    if pdf.is_encrypted:
        enc = pdf.encryption
        ctx.info("structure:encrypted", P, "Encrypted", True, cls=RECONSTRUCT,
                 note="Reproduced with the 'Encryption' output option (passwords cannot be recovered from a file).")
        for label, fn in (("Security handler revision (R)", lambda: enc.R), ("Algorithm version (V)", lambda: enc.V),
                          ("Key length (bits)", lambda: enc.bits),
                          ("Stream method", lambda: str(enc.stream_method).split(".")[-1]),
                          ("String method", lambda: str(enc.string_method).split(".")[-1]),
                          ("Permissions (P)", lambda: enc.P),
                          ("Opened with", lambda: "owner password" if pdf.owner_password_matched else
                           ("user password" if pdf.user_password_matched else "?"))):
            try:
                ctx.info(f"structure:enc:{label}", P, label, fn(), cls=READONLY)
            except Exception as e:
                ctx.info(f"structure:enc:{label}", P, label, None, error=str(e))
        allow = pdf.allow
        ctx.info("structure:enc:allow", P, "Permissions granted",
                 ", ".join(k for k, v in allow._asdict().items() if v) or "none", cls=READONLY)
        encdict = pdf.trailer.get("/Encrypt")
        if is_obj(encdict, Dictionary) and "/EncryptMetadata" in encdict:
            ctx.info("structure:enc:meta", P, "Encrypt metadata", bool(encdict["/EncryptMetadata"]), cls=READONLY)
    else:
        ctx.info("structure:encrypted", P, "Encrypted", False, cls=RECONSTRUCT)

    P = ["Structure", "File structure"]
    ctx.info("structure:linearized", P, "Linearized (fast web view)", pdf.is_linearized, cls=RECONSTRUCT,
             note="Reproduced by the 'Linearize' output option.")
    objstm = sum(1 for o in pdf.objects if is_obj(o, Stream) and o.stream_dict.get("/Type") == Name.ObjStm)
    e = ctx.info("structure:object_streams", P, "Object streams (/ObjStm)",
                 f"Yes ({objstm} object streams)" if objstm else "No", cls=RECONSTRUCT,
                 note="Use of object streams is reproduced by the 'Object streams' output option; how many "
                      "the writer creates depends on the content.")
    e["canon"] = "v:yes" if objstm else "v:no"
    xref_kind = "cross-reference stream" if ctx.pdf.trailer.get("/Type") == Name.XRef else "classic table"
    if any(s.get("XRefStm") for s in raw["sections"]):
        xref_kind = "hybrid (table + stream)"
    ctx.info("structure:xref", P, "Cross-reference format", xref_kind, cls=REGENERATE,
             note="Follows from the object-stream setting.")
    filters: Counter = Counter()
    nstreams = 0
    for o in pdf.objects:
        if is_obj(o, Stream):
            nstreams += 1
            f = o.stream_dict.get("/Filter")
            if f is None:
                filters["(uncompressed)"] += 1
            elif is_obj(f, Array):
                filters[" + ".join(str(x) for x in f)] += 1
            else:
                filters[str(f)] += 1
    ctx.info("structure:compression", P, "Stream compression",
             f"{nstreams:,} streams: " + ", ".join(f"{k} × {v}" for k, v in filters.most_common()), cls=READONLY,
             note="Each stream keeps its own encoding; content streams of the second PDF are not re-encoded "
                  "unless 'Compress uncompressed streams' is chosen.")
    ctx.info("structure:object_count", P, "Object count", len(pdf.objects), cls=REGENERATE)
    ctx.info("structure:file_size", P, "File size", f"{raw['size']:,} bytes", cls=REGENERATE)
    ctx.info("structure:sha256", P, "SHA-256 of file", hashlib.sha256(ctx.data).hexdigest(), cls=REGENERATE,
             note="Byte-level identity cannot be reproduced: the output contains different content.")
    if raw["trailing_bytes"]:
        ctx.info("structure:trailing", P, "Bytes after last %%EOF", raw["trailing_bytes"], cls=REGENERATE)
    warnings = ctx.warnings + _warnings(pdf)  # type: ignore[attr-defined]
    ctx.warnings = warnings  # type: ignore[attr-defined]
    ctx.info("structure:warnings", P, "Parser warnings (file damage)",
             "\n".join(warnings[:20]) + (f"\n… {len(warnings) - 20} more" if len(warnings) > 20 else "")
             if warnings else None, cls=READONLY)

    P = ["Structure", "Object numbering"]
    note = "Kept by the 'Object numbers' output option (dittopdf writes the file itself)."

    def objnum(id: str, label: str, o: Any) -> None:
        ok = is_obj(o, pikepdf.Object) and o.is_indirect
        ctx.info(id, P, label, f"{o.objgen[0]} {o.objgen[1]}" if ok else None, cls=RECONSTRUCT, note=note)

    objnum("structure:objnum:root", "Catalog object number", pdf.Root)
    objnum("structure:objnum:info", "Info dictionary object number", pdf.trailer.get("/Info"))
    objnum("structure:objnum:pages", "Page tree root object number", pdf.Root.get("/Pages"))
    objnum("structure:objnum:metadata", "XMP stream object number", pdf.Root.get("/Metadata"))
    ctx.info("structure:objnum:size", P, "Trailer /Size", int(pdf.trailer.get("/Size", 0) or 0) or None,
             cls=RECONSTRUCT, note="One more than the highest object number.")
    membership = numbering.objstm_membership(pdf)
    if membership:
        groups: dict[int, list[int]] = {}
        for n, k in sorted(membership.items()):
            groups.setdefault(k, []).append(n)
        e = ctx.info("structure:objnum:objstm", P, "Objects inside object streams",
                     "\n".join(f"stream {k}: objects {_ranges(set(ns))}" for k, ns in sorted(groups.items())),
                     cls=RECONSTRUCT, note=note)
        e["canon"] = "v:" + ";".join(f"{n}>{k}" for n, k in sorted(membership.items()))
    else:
        ctx.info("structure:objnum:objstm", P, "Objects inside object streams", None, cls=RECONSTRUCT, note=note)
    xs = numbering.xref_streams(pdf)
    ctx.info("structure:objnum:xref", P, "Cross-reference data",
             f"stream, object {xs[-1]}" if xs else "classic table", cls=RECONSTRUCT, note=note)

    P = ["Structure", "Revisions"]
    secs = raw["sections"]
    revisions = raw["eof_markers"] - (1 if pdf.is_linearized and raw["eof_markers"] > 1 else 0)
    ctx.info("structure:revisions", P, "Number of revisions (%%EOF markers)", max(revisions, 1), cls=REGENERATE,
             note="The output is written as a single revision.")
    ctx.info("structure:incremental", P, "Incremental updates", max(revisions - 1, 0), cls=REGENERATE)
    chain = " → ".join(f"{s['type']} @ {s['offset']:,}" for s in secs)
    ctx.info("structure:prev_chain", P, "Cross-reference /Prev chain (newest first)", chain or None,
             cls=REGENERATE, error=None)
    if raw["chain_error"]:
        ctx.info("structure:chain_error", P, "Chain problem", raw["chain_error"], cls=READONLY,
                 warn="The cross-reference chain is damaged; qpdf reconstructed it.")


def _escape(data: bytes) -> str:
    """Bytes as text with line endings and non-ASCII bytes shown escaped."""
    out = []
    for b in data:
        if b == 0x0D:
            out.append("\\r")
        elif b == 0x0A:
            out.append("\\n")
        elif 32 <= b < 127:
            out.append(chr(b))
        else:
            out.append(f"\\x{b:02x}")
    return "".join(out)


def _size_text(w: Any, h: Any, rot: int) -> str:
    if not isinstance(w, float):
        return "unknown"
    r = f", rotated {rot}°" if rot else ""
    return f"{w:g}×{h:g} pt ({w / 72:.2f}×{h / 72:.2f} in){r}"


def _effective(page: Any, key: str) -> tuple[Any, bool]:
    if key in page:
        return page[key], False
    if key in INHERITABLE:
        node, hops = page.get("/Parent"), 0
        while node is not None and hops < 50:
            if key in node:
                return node[key], True
            node, hops = node.get("/Parent"), hops + 1
    return None, False


def _pages(ctx: Ctx) -> None:
    pages = ctx.pdf.pages
    for i, page in enumerate(pages, 1):
        if i > ctx.max_pages:
            ctx.info("page:__truncated__", ["Pages", "More pages"], "Pages not shown in detail",
                     f"{len(pages) - ctx.max_pages:,} more pages", cls=READONLY,
                     note="Raise DITTOPDF_MAX_DETAIL_PAGES to inspect them; copying still applies the "
                          "document-level settings.")
            break
        po = page.obj
        P = ["Pages", f"Page {i}"]
        try:
            mb = [float(x) for x in page.mediabox]
            ctx.info(f"page:{i}:size", P, "Page size", _size_text(mb[2] - mb[0], mb[3] - mb[1],
                                                                 int(po.get("/Rotate", 0) or 0)), cls=READONLY)
        except Exception as e:
            ctx.info(f"page:{i}:size", P, "Page size", None, error=str(e))
        ctx.info(f"page:{i}:objnum", P, "Object number", f"{po.objgen[0]} {po.objgen[1]}", cls=RECONSTRUCT,
                 note="Kept by the 'Object numbers' output option.")
        for k, spec in PAGE.items():
            v, inherited = _effective(po, k)
            if k == "/Parent":
                ctx.info(f"page:{i}:{k}", P, k, "→ page tree node" if v is not None else None, cls=REGENERATE,
                         note=spec[3])
                continue
            if k == "/Type":
                ctx.info(f"page:{i}:{k}", P, k, str(v) if v is not None else None, cls=REGENERATE)
                continue
            if k == "/Annots":
                ctx.info(f"page:{i}:{k}", P, k, f"{len(v)} annotation(s)" if is_obj(v, Array) else None,
                         cls=READONLY, note=spec[3])
                continue
            kw = spec_kw(spec)
            if inherited:
                kw["note"] = (kw["note"] + " Inherited from the page tree; written on the page itself.").strip()
            ctx.obj(f"page:{i}:{k}", P, k, v, choices=CHOICES.get(k), **kw)
        for k in sorted(set(po.keys()) - set(PAGE) - PAGE_CONTENT_KEYS):
            ctx.obj(f"page:{i}:{k}", P, k, po.get(k), cls=RECONSTRUCT, editable=False,
                    note="Non-standard page key; copied as-is.")


def _annotations(ctx: Ctx) -> None:
    count = 0
    for i, page in enumerate(ctx.pdf.pages, 1):
        annots = page.obj.get("/Annots")
        if not is_obj(annots, Array):
            continue
        for j, a in enumerate(annots, 1):
            count += 1
            if count > 3000 or i > ctx.max_pages:
                ctx.info("annot:__truncated__", ["Annotations", "More annotations"], "Not shown in detail",
                         "Too many annotations to list individually", cls=READONLY)
                return
            if not is_obj(a, Dictionary):
                ctx.add(entry(f"annot:{i}:{j}:/Subtype", ["Annotations", f"Page {i} → Annotation {j}"],
                              "/Subtype", error="Annotation is not a dictionary"))
                continue
            sub = str(a.get("/Subtype", "?"))
            P = ["Annotations", f"Page {i} → Annotation {j} ({sub[1:]})"]
            keys = ANNOT_COMMON + (ANNOT_MARKUP if sub in MARKUP_SUBTYPES else [])
            keys += sorted(set(a.keys()) - set(keys))
            for k in keys:
                v = a.get(k)
                id = f"annot:{i}:{j}:{k}"
                if k in ANNOT_REGEN:
                    ctx.info(id, P, k, ctx.ref_display(v) if v is not None else None, cls=REGENERATE)
                elif k in ("/Popup", "/IRT", "/Parent"):
                    e = ctx.add(info_entry(id, P, k, ctx.ref_display(v) if v is not None else None,
                                           cls=RECONSTRUCT if k != "/Parent" else READONLY,
                                           note="Relationship is re-linked to the corresponding annotation."
                                           if k != "/Parent" else "Form field this widget belongs to."))
                    if k != "/Parent":
                        e["actions"], e["default"] = [COPY, KEEP, REMOVE], COPY
                elif k in ANNOT_DIRECT:
                    ctx.obj(id, P, k, v, cls=DIRECT, editable=True,
                            absent_kind="date" if k in ("/M", "/CreationDate") else "text"
                            if k in ("/Contents", "/T", "/Subj", "/NM") else None)
                elif k in ANNOT_RECON:
                    ctx.obj(id, P, k, v, cls=RECONSTRUCT, editable=False)
                elif k == "/StructParent":
                    ctx.obj(id, P, k, v, cls=UNREPRODUCIBLE, actions=[], default=KEEP,
                            note="Follows the 'Structure tree' output option.")
                else:
                    ctx.obj(id, P, k, v, cls=READONLY,
                            note="Geometry/appearance of the annotation; copied only with "
                                 "'Replace annotations' in the output options.")


def _signatures(ctx: Ctx) -> None:
    P0 = ["Signatures", "About signatures"]
    ctx.info("sig:note", P0, "Copy policy",
             "Signatures are read-only diagnostic data. A signature covers the exact bytes of the signed "
             "file; it cannot be moved to another PDF, and rewriting a signed PDF invalidates it. "
             "Re-signing requires the signer's private key.", cls=READONLY)
    n = 0
    for name, field, sig in signatures.iter_signature_fields(ctx.pdf):
        n += 1
        P = ["Signatures", f"Signature {n}: {name}"]
        if not is_obj(sig, Dictionary):
            ctx.info(f"sig:{n}:state", P, "State", "Unsigned signature field", cls=READONLY)
            continue
        ctx.info(f"sig:{n}:state", P, "State", "Signed", cls=READONLY)
        for k in signatures.SIG_KEYS:
            v = sig.get(k)
            id = f"sig:{n}:{k}"
            if k == "/Contents" and is_obj(v, String):
                b = bytes(v)
                ctx.add(entry(id, P, k, display=f"{len(signatures.der_slice(b)):,} bytes of signature data "
                              f"({len(b):,} bytes reserved)", canon="s:" + pdfobj.sha256_hex(b), cls=READONLY))
                continue
            ctx.obj(id, P, k, v, cls=READONLY)
        cov = signatures.byte_range_coverage(sig, len(ctx.data))
        ctx.info(f"sig:{n}:coverage", P, "Byte range coverage", cov, cls=READONLY)
        ctx.info(f"sig:{n}:verified", P, "Cryptographic validity", "Not verified by this tool", cls=READONLY)
        certs, err = signatures.certificates(sig)
        if err:
            ctx.info(f"sig:{n}:certs", P, "Certificates", None, error=err)
        for m, cert in enumerate(certs, 1):
            for label, val in cert.items():
                ctx.info(f"sig:{n}:cert{m}:{label}", P + [f"Certificate {m}"], label, val, cls=READONLY)
    if n == 0:
        ctx.info("sig:none", P0, "Signature fields", None, cls=READONLY)


def _fonts(ctx: Ctx) -> None:
    res = ctx.res
    ctx.info("font:note", ["Fonts", "About fonts"], "Copy policy",
             "Font metadata is diagnostic. Fonts belong to the page content they render: replacing the second "
             "PDF's fonts or their descriptors with the original's would break its text.", cls=READONLY)
    if res is None or not res.fonts:
        ctx.info("font:none", ["Fonts", "About fonts"], "Fonts found", None, cls=READONLY)
        return
    counter: Counter = Counter()
    for found in sorted(res.fonts.values(), key=lambda f: (str(f.obj.get("/BaseFont", "")), min(f.pages))):
        font = found.obj
        base = str(font.get("/BaseFont", "/(unnamed)"))[1:]
        stripped = fontmod.SUBSET_RE.sub("", base)
        counter[stripped] += 1
        fid = f"font:{stripped}#{counter[stripped]}"
        sub = str(font.get("/Subtype", "?"))[1:]
        G = ["Fonts", f"{base} ({sub})" + (f" #{counter[stripped]}" if counter[stripped] > 1 else "")]
        PD = G + ["Font dictionary"]
        for k in ["/BaseFont", "/Subtype", "/Encoding", "/ToUnicode", "/FirstChar", "/LastChar", "/Widths",
                  "/DescendantFonts", "/FontMatrix", "/CharProcs"]:
            v = font.get(k)
            if k == "/Widths" and is_obj(v, Array):
                ctx.info(f"{fid}:dict:{k}", PD, k, f"{len(v)} widths", cls=READONLY)
            elif k == "/ToUnicode" and is_obj(v, Stream):
                ctx.info(f"{fid}:dict:{k}", PD, k, pdfobj.stream_summary(v), cls=READONLY,
                         note="Character-to-Unicode map (text extraction / copy-paste).")
            elif k == "/CharProcs" and is_obj(v, Dictionary):
                ctx.info(f"{fid}:dict:{k}", PD, k, f"{len(v)} glyph procedures", cls=READONLY)
            else:
                ctx.obj(f"{fid}:dict:{k}", PD, k, v, cls=READONLY)
        desc = font.get("/DescendantFonts")
        if is_obj(desc, Array) and len(desc):
            d0 = desc[0]
            for k in ["/Subtype", "/BaseFont", "/CIDSystemInfo", "/CIDToGIDMap", "/DW"]:
                v = d0.get(k)
                if v is not None:
                    disp = pdfobj.stream_summary(v) if is_obj(v, Stream) else None
                    if disp:
                        ctx.info(f"{fid}:cid:{k}", PD, f"Descendant {k}", disp, cls=READONLY)
                    else:
                        ctx.obj(f"{fid}:cid:{k}", PD, f"Descendant {k}", v, cls=READONLY)
        fd = fontmod.descriptor_of(font)
        PF = G + ["Font descriptor"]
        if is_obj(fd, Dictionary):
            for k in FD_KEYS:
                v = fd.get(k)
                if k in ("/CIDSet",) and is_obj(v, Stream):
                    ctx.info(f"{fid}:fd:{k}", PF, k, pdfobj.stream_summary(v), cls=READONLY)
                else:
                    ctx.obj(f"{fid}:fd:{k}", PF, k, v, cls=READONLY)
        else:
            ctx.info(f"{fid}:fd", PF, "Font descriptor", None, cls=READONLY,
                     note="Standard 14 fonts and Type 3 fonts may have no descriptor.")
        PP = G + ["Embedded font program"]
        key, prog = fontmod.program_of(fd)
        ctx.info(f"{fid}:prog:embedded", PP, "Embedded", bool(prog), cls=READONLY)
        ctx.info(f"{fid}:prog:subset", PP, "Subset (ABCDEF+ prefix)",
                 f"Yes ({base[:6]})" if fontmod.SUBSET_RE.match(base) else "No", cls=READONLY)
        if prog is not None:
            for label, val in fontmod.analyse_program(key, prog).items():
                if label == "error":
                    ctx.info(f"{fid}:prog:error", PP, "Program parsing", None, error=val)
                else:
                    ctx.info(f"{fid}:prog:{label}", PP, label, val, cls=READONLY)
        PU = G + ["Usage"]
        ctx.info(f"{fid}:use:pages", PU, "Used on pages", _ranges(found.pages), cls=READONLY)
        ctx.info(f"{fid}:use:names", PU, "Resource names", ", ".join(sorted(found.names)), cls=READONLY)
        ctx.info(f"{fid}:use:via", PU, "Referenced from", ", ".join(sorted(found.via)), cls=READONLY)


def _ranges(nums: set[int]) -> str:
    out, run = [], []
    for n in sorted(nums):
        if run and n == run[-1] + 1:
            run.append(n)
        else:
            if run:
                out.append(f"{run[0]}–{run[-1]}" if len(run) > 1 else str(run[0]))
            run = [n]
    if run:
        out.append(f"{run[0]}–{run[-1]}" if len(run) > 1 else str(run[0]))
    return ", ".join(out)


def _images(ctx: Ctx) -> None:
    res = ctx.res
    P0 = ["Images", "About images"]
    ctx.info("image:note", P0, "Copy policy",
             "Image properties describe the second PDF's own images and are diagnostic. An image's XMP "
             "(/Metadata) is copied only onto an image whose pixel data is identical to the original's.",
             cls=READONLY)
    if res is None or not res.images:
        ctx.info("image:none", P0, "Image XObjects found", None, cls=READONLY)
    if res is not None and res.inline_images:
        ctx.info("image:inline", P0, "Inline images",
                 "; ".join(f"p.{x['page']} {x['width']}×{x['height']} px" +
                           (f" @ {x['dpi_x']:g}×{x['dpi_y']:g} dpi" if "dpi_x" in x else "")
                           for x in res.inline_images[:50]), cls=READONLY)
    if res is None:
        return
    for n, found in enumerate(res.images.values(), 1):
        x = found.obj
        w, h = x.get("/Width", "?"), x.get("/Height", "?")
        filt = x.get("/Filter")
        ftxt = brief(filt) if filt is not None else "uncompressed"
        pages = _ranges(found.pages) if found.pages else "?"
        G = ["Images", f"Image {n}: {w}×{h} px, {ftxt} (p. {pages})"]
        PD = G + ["Image dictionary"]
        for k in IMAGE_KEYS:
            v = x.get(k)
            id = f"image:{n}:{k}"
            if k == "/Metadata":
                ctx.obj(id, PD, k, v, cls=RECONSTRUCT, editable=False,
                            note="Per-image XMP; copied only to an identical image in the second PDF.")
                continue
            if k in ("/SMask", "/Mask") and is_obj(v, Stream):
                ctx.info(id, PD, k, f"{pdfobj.ref_str(v)}: {v.get('/Width')}×{v.get('/Height')} mask image",
                         cls=READONLY)
                continue
            ctx.obj(id, PD, k, v, cls=READONLY)
        try:
            rawdata = x.read_raw_bytes()
            ctx.info(f"image:{n}:size", PD, "Encoded data size", f"{len(rawdata):,} bytes", cls=READONLY)
            ctx.info(f"image:{n}:sha", PD, "Encoded data SHA-256", pdfobj.sha256_hex(rawdata), cls=READONLY)
        except Exception as ex:
            rawdata = b""
            ctx.info(f"image:{n}:sha", PD, "Encoded data", None, error=str(ex))
        PU = G + ["Placement & resolution"]
        ctx.info(f"image:{n}:pages", PU, "Used on pages", pages, cls=READONLY)
        ctx.info(f"image:{n}:via", PU, "Referenced from", ", ".join(sorted(found.via)), cls=READONLY)
        if found.placements:
            for m, pl in enumerate(found.placements[:20], 1):
                dpi = f" → {pl['dpi_x']:g} × {pl['dpi_y']:g} dpi" if "dpi_x" in pl else ""
                ctx.info(f"image:{n}:place{m}", PU, f"Placement {m}",
                         f"page {pl['page']}: {pl['width_pt']:g} × {pl['height_pt']:g} pt "
                         f"({pl['width_pt'] / 72:.2f} × {pl['height_pt'] / 72:.2f} in){dpi}", cls=READONLY)
        else:
            ctx.info(f"image:{n}:place", PU, "Placement", None, cls=READONLY,
                     note="Not drawn directly by page content (mask, pattern, annotation or unused); "
                          "resolution cannot be derived.")
        md = x.get("/Metadata")
        if is_obj(md, Stream):
            parsed = xmp.parse(md.read_bytes())
            PX = G + ["Image XMP"]
            if parsed.get("error"):
                ctx.info(f"image:{n}:xmp", PX, "XMP", None, error=parsed["error"])
            for p in parsed["props"]:
                ctx.add(entry(f"image:{n}:xmp:{p['uri']}#{p['local']}", PX, f"{p['prefix']}:{p['local']}",
                              display=xmp.display(p["value"]), canon=xmp.canonical(p["value"]), cls=READONLY))
        if filt == Name.DCTDecode and rawdata and len(rawdata) < 60_000_000:
            _jpeg_meta(ctx, n, G + ["Embedded EXIF / IPTC (JPEG)"], rawdata)


def _jpeg_meta(ctx: Ctx, n: int, P: list[str], data: bytes) -> None:
    try:
        from PIL import ExifTags, Image, IptcImagePlugin

        im = Image.open(io.BytesIO(data))
        exif = im.getexif()
        found = False
        for tag, val in list(exif.items())[:80]:
            found = True
            name = ExifTags.TAGS.get(tag, f"Tag {tag}")
            ctx.info(f"image:{n}:exif:{name}", P, f"EXIF {name}", _short(val), cls=READONLY)
        try:
            gps = exif.get_ifd(0x8825)
            for tag, val in gps.items():
                found = True
                ctx.info(f"image:{n}:gps:{tag}", P, f"GPS {ExifTags.GPSTAGS.get(tag, tag)}", _short(val),
                         cls=READONLY)
        except Exception:
            pass
        iptc = IptcImagePlugin.getiptcinfo(im) or {}
        for (rec, ds), val in list(iptc.items())[:80]:
            found = True
            ctx.info(f"image:{n}:iptc:{rec}:{ds}", P, f"IPTC {rec}:{ds}", _short(val), cls=READONLY)
        if im.info.get("xmp"):
            found = True
            ctx.info(f"image:{n}:jpegxmp", P, "XMP inside JPEG (APP1)", f"{len(im.info['xmp']):,} bytes",
                     cls=READONLY)
        if im.info.get("icc_profile"):
            found = True
            ctx.info(f"image:{n}:jpegicc", P, "ICC profile inside JPEG", f"{len(im.info['icc_profile']):,} bytes",
                     cls=READONLY)
        if not found:
            ctx.info(f"image:{n}:exif", P, "EXIF / IPTC / XMP in JPEG data", None, cls=READONLY)
    except Exception as e:
        ctx.info(f"image:{n}:exif", P, "EXIF / IPTC", None, error=f"Unable to read JPEG metadata: {e}")


def _short(val: Any) -> str:
    if isinstance(val, bytes):
        txt = val.decode("utf-8", "replace") if all(32 <= b < 127 for b in val[:64]) else val[:32].hex() + "…"
        return txt[:300]
    if isinstance(val, (list, tuple)):
        return ", ".join(_short(v) for v in val)[:300]
    return str(val)[:300]


def _filespecs(ctx: Ctx) -> list[tuple[str, Any, str]]:
    out: list[tuple[str, Any, str]] = []
    seen: set = set()

    def add(name: str, fs: Any, via: str) -> None:
        if not is_obj(fs, Dictionary):
            return
        k = fs.objgen if fs.is_indirect else id(fs)
        if k in seen:
            return
        seen.add(k)
        out.append((name, fs, via))

    names = ctx.pdf.Root.get("/Names")
    if is_obj(names, Dictionary) and "/EmbeddedFiles" in names:
        for name, fs in pikepdf.NameTree(names["/EmbeddedFiles"]).items():
            add(name, fs, "/Names /EmbeddedFiles")
    for fs in ctx.pdf.Root.get("/AF", []) or []:
        add(_fs_name(fs), fs, "catalog /AF")
    for i, page in enumerate(ctx.pdf.pages, 1):
        for fs in page.obj.get("/AF", []) or []:
            add(_fs_name(fs), fs, f"page {i} /AF")
        for a in page.obj.get("/Annots", []) or []:
            if is_obj(a, Dictionary) and a.get("/Subtype") == Name.FileAttachment:
                add(_fs_name(a.get("/FS")), a.get("/FS"), f"file attachment annotation, page {i}")
    return out


def _fs_name(fs: Any) -> str:
    if not is_obj(fs, Dictionary):
        return "?"
    for k in ("/UF", "/F"):
        if k in fs:
            return pdfobj.decode_text(bytes(fs[k])) or "?"
    return "?"


def _embedded(ctx: Ctx) -> None:
    P0 = ["Embedded files", "About embedded files"]
    ctx.info("ef:note", P0, "Copy policy",
             "Embedded files are copied with Catalog → /Names/EmbeddedFiles, Catalog → /AF, page /AF, and "
             "(with 'Replace annotations') file-attachment annotations. Their relationships are preserved.",
             cls=READONLY)
    specs = _filespecs(ctx)
    if not specs:
        ctx.info("ef:none", P0, "Embedded files", None, cls=READONLY)
    for n, (name, fs, via) in enumerate(specs, 1):
        P = ["Embedded files", f"File {n}: {name}"]
        ctx.info(f"ef:{n}:via", P, "Referenced from", via, cls=READONLY)
        for k in ("/Type", "/F", "/UF", "/Desc", "/AFRelationship"):
            ctx.obj(f"ef:{n}:{k}", P, f"File spec {k}", fs.get(k), cls=READONLY)
        ef = fs.get("/EF")
        stream = ef.get("/UF", ef.get("/F")) if is_obj(ef, Dictionary) else None
        if not is_obj(stream, Stream):
            ctx.info(f"ef:{n}:stream", P, "Embedded file stream", None, cls=READONLY,
                     note="File specification without embedded data (external reference).")
            continue
        ctx.obj(f"ef:{n}:/Subtype", P, "Stream /Subtype (MIME type)", stream.get("/Subtype"), cls=READONLY)
        params = stream.get("/Params") if is_obj(stream.get("/Params"), Dictionary) else Dictionary()
        for k in ("/CreationDate", "/ModDate", "/Size", "/CheckSum"):
            ctx.obj(f"ef:{n}:params{k}", P, f"Params {k}", params.get(k), cls=READONLY)
        try:
            data = stream.read_bytes()
            ctx.info(f"ef:{n}:actual_size", P, "Actual size", f"{len(data):,} bytes", cls=READONLY)
            md5 = hashlib.md5(data).digest()
            ctx.info(f"ef:{n}:sha256", P, "SHA-256 of contents", pdfobj.sha256_hex(data), cls=READONLY)
            if "/CheckSum" in params:
                ok = bytes(params["/CheckSum"]) == md5
                ctx.info(f"ef:{n}:checksum_ok", P, "/CheckSum matches contents", ok, cls=READONLY,
                         warn="" if ok else "Stored MD5 does not match the data.")
        except Exception as e:
            ctx.info(f"ef:{n}:data", P, "Contents", None, error=f"Unable to decode: {e}")
    coll = ctx.pdf.Root.get("/Collection")
    if is_obj(coll, Dictionary):
        P = ["Embedded files", "Collection (PDF portfolio)"]
        for k in ("/View", "/D", "/Sort", "/Navigator", "/Colors", "/Folders", "/Split"):
            ctx.obj(f"collection:{k}", P, k, coll.get(k), cls=READONLY)
        schema = coll.get("/Schema")
        if is_obj(schema, Dictionary):
            for fk, fv in schema.items():
                if fk == "/Type" or not is_obj(fv, Dictionary):
                    continue
                ctx.info(f"collection:schema:{fk}", P, f"/Schema {fk}",
                         f"name {brief(fv.get('/N'))}, type {brief(fv.get('/Subtype'))}, order "
                         f"{brief(fv.get('/O'))}, visible {brief(fv.get('/V', True))}", cls=READONLY)
        ctx.info("collection:copy", P, "How it is copied", "Catalog → /Collection", cls=READONLY)


def _pieceinfo(ctx: Ctx) -> None:
    locs: list[tuple[str, str, Any]] = []
    if "/PieceInfo" in ctx.pdf.Root:
        locs.append(("doc", "Document (catalog)", ctx.pdf.Root["/PieceInfo"]))
    for i, page in enumerate(ctx.pdf.pages, 1):
        if i > ctx.max_pages:
            break
        if "/PieceInfo" in page.obj:
            locs.append((f"p{i}", f"Page {i}", page.obj["/PieceInfo"]))
    if ctx.res:
        for n, (label, pi) in enumerate(ctx.res.forms_pieceinfo, 1):
            locs.append((f"x{n}", label.capitalize(), pi))
    P0 = ["PieceInfo", "About PieceInfo"]
    ctx.info("piece:note", P0, "Copy policy",
             "Application private data is copied unchanged (including unknown applications) via Catalog → "
             "/PieceInfo and each page's /PieceInfo. Form XObject PieceInfo is part of the content.",
             cls=READONLY)
    if not locs:
        ctx.info("piece:none", P0, "PieceInfo dictionaries", None, cls=READONLY)
    for loc, label, pi in locs:
        if not is_obj(pi, Dictionary):
            ctx.info(f"piece:{loc}", ["PieceInfo", label], "/PieceInfo", None, error="Not a dictionary")
            continue
        for app, d in pi.items():
            P = ["PieceInfo", label, f"Application {app}"]
            if not is_obj(d, Dictionary):
                ctx.obj(f"piece:{loc}:{app}", P, "Value", d, cls=READONLY)
                continue
            ctx.obj(f"piece:{loc}:{app}:/LastModified", P, "/LastModified", d.get("/LastModified"), cls=READONLY)
            priv = d.get("/Private")
            if priv is not None:
                size = ""
                if is_obj(priv, Dictionary):
                    size = f"{len(priv)} keys: " + ", ".join(list(priv.keys())[:12])
                ctx.add(obj_entry(f"piece:{loc}:{app}:/Private", P, "/Private", priv, cls=READONLY,
                                  pages=ctx.pages, note=size))
            else:
                ctx.info(f"piece:{loc}:{app}:/Private", P, "/Private", None, cls=READONLY)
            for k in sorted(set(d.keys()) - {"/LastModified", "/Private"}):
                ctx.obj(f"piece:{loc}:{app}:{k}", P, k, d.get(k), cls=READONLY)


def _icc(data: bytes) -> dict[str, Any]:
    out: dict[str, Any] = {"Profile size": f"{len(data):,} bytes", "Profile SHA-256": pdfobj.sha256_hex(data)}
    if len(data) >= 128:
        out["ICC version"] = f"{data[8]}.{data[9] >> 4}.{data[9] & 0xF}"
        out["Device class"] = data[12:16].decode("latin-1").strip()
        out["Colour space"] = data[16:20].decode("latin-1").strip()
        out["PCS"] = data[20:24].decode("latin-1").strip()
    try:
        from PIL import ImageCms

        prof = ImageCms.ImageCmsProfile(io.BytesIO(data))
        out["Description"] = ImageCms.getProfileDescription(prof).strip()
        cp = ImageCms.getProfileCopyright(prof).strip()
        if cp:
            out["Copyright"] = cp
    except Exception:
        pass
    return out


def _output_intents(ctx: Ctx) -> None:
    groups: list[tuple[str, Any]] = []
    ois = ctx.pdf.Root.get("/OutputIntents")
    if is_obj(ois, Array):
        groups += [(f"doc:{n}", oi) for n, oi in enumerate(ois, 1)]
    for i, page in enumerate(ctx.pdf.pages, 1):
        if i > ctx.max_pages:
            break
        pois = page.obj.get("/OutputIntents")
        if is_obj(pois, Array):
            groups += [(f"p{i}:{n}", oi) for n, oi in enumerate(pois, 1)]
    P0 = ["Output intents", "About output intents"]
    ctx.info("oi:note", P0, "Copy policy",
             "Output intents and their ICC profiles are copied via Catalog → /OutputIntents and page "
             "/OutputIntents.", cls=READONLY)
    if not groups:
        ctx.info("oi:none", P0, "Output intents", None, cls=READONLY)
    for key, oi in groups:
        if not is_obj(oi, Dictionary):
            continue
        where = "Document" if key.startswith("doc") else f"Page {key[1:].split(':')[0]}"
        P = ["Output intents", f"{where} output intent {key.split(':')[-1]} ({brief(oi.get('/S'))})"]
        for k in ("/Type", "/S", "/OutputConditionIdentifier", "/OutputCondition", "/RegistryName", "/Info",
                  "/DestOutputProfileRef", "/MixingHints", "/SpectralData"):
            ctx.obj(f"oi:{key}:{k}", P, k, oi.get(k), cls=READONLY)
        prof = oi.get("/DestOutputProfile")
        if is_obj(prof, Stream):
            ctx.info(f"oi:{key}:/DestOutputProfile", P, "/DestOutputProfile", pdfobj.stream_summary(prof),
                     cls=READONLY)
            try:
                for label, val in _icc(prof.read_bytes()).items():
                    ctx.info(f"oi:{key}:icc:{label}", P + ["Embedded ICC profile"], label, val, cls=READONLY)
            except Exception as e:
                ctx.info(f"oi:{key}:icc", P, "ICC profile", None, error=str(e))
        else:
            ctx.info(f"oi:{key}:/DestOutputProfile", P, "/DestOutputProfile", None, cls=READONLY)


def _measure_dict(ctx: Ctx, id: str, P: list[str], m: Any) -> None:
    if not is_obj(m, Dictionary):
        return
    for k in ("/Type", "/Subtype", "/R", "/O", "/Bounds", "/DCS", "/PDU", "/GPTS", "/LPTS"):
        if k in m:
            ctx.obj(f"{id}:{k}", P, f"Measure {k}", m[k], cls=READONLY)
    for k, label in (("/X", "X-axis units"), ("/Y", "Y-axis units"), ("/D", "Distance units"),
                     ("/A", "Area units"), ("/T", "Angle units"), ("/S", "Slope units")):
        fmts = m.get(k)
        if is_obj(fmts, Array):
            ctx.info(f"{id}:{k}", P, f"Measure {k} ({label})",
                     "; ".join(f"{brief(f.get('/U'))} × {brief(f.get('/C'))}" for f in fmts
                               if is_obj(f, Dictionary)), cls=READONLY)
    gcs = m.get("/GCS")
    if is_obj(gcs, Dictionary):
        for k in ("/Type", "/EPSG", "/WKT"):
            if k in gcs:
                ctx.obj(f"{id}:gcs{k}", P, f"Coordinate system {k}", gcs[k], cls=READONLY)
        wkt = pdfobj.decode_text(bytes(gcs["/WKT"])) if is_obj(gcs.get("/WKT"), String) else ""
        if "DATUM[" in wkt:
            datum = wkt.split("DATUM[", 1)[1].split(",", 1)[0].strip('"')
            ctx.info(f"{id}:datum", P, "Datum (from WKT)", datum, cls=READONLY)


def _measure(ctx: Ctx) -> None:
    found = False
    for i, page in enumerate(ctx.pdf.pages, 1):
        if i > ctx.max_pages:
            break
        vps = page.obj.get("/VP")
        if not is_obj(vps, Array):
            continue
        for j, vp in enumerate(vps, 1):
            if not is_obj(vp, Dictionary):
                continue
            found = True
            P = ["Measurement & geospatial", f"Page {i} viewport {j}"]
            for k in ("/Type", "/BBox", "/Name"):
                ctx.obj(f"geo:p{i}:vp{j}:{k}", P, k, vp.get(k), cls=READONLY)
            if "/PtData" in vp:
                ctx.obj(f"geo:p{i}:vp{j}:/PtData", P, "/PtData", vp["/PtData"], cls=READONLY)
            _measure_dict(ctx, f"geo:p{i}:vp{j}", P, vp.get("/Measure"))
    if ctx.res:
        for n, f in enumerate(ctx.res.images.values(), 1):
            if "/Measure" in f.obj or "/PtData" in f.obj:
                found = True
                P = ["Measurement & geospatial", f"Image {n}"]
                _measure_dict(ctx, f"geo:img{n}", P, f.obj.get("/Measure"))
                if "/PtData" in f.obj:
                    ctx.obj(f"geo:img{n}:/PtData", P, "/PtData", f.obj["/PtData"], cls=READONLY)
    P0 = ["Measurement & geospatial", "About measurement data"]
    ctx.info("geo:note", P0, "Copy policy",
             "Viewports are copied with each page's /VP entry (Pages section). Measurement data attached to "
             "images belongs to those images.", cls=READONLY)
    if not found:
        ctx.info("geo:none", P0, "Viewports / measurement dictionaries", None, cls=READONLY)


def _dparts(ctx: Ctx) -> None:
    root = ctx.pdf.Root.get("/DPartRoot")
    P = ["Document parts", "Document-part hierarchy"]
    ctx.info("dpart:note", P, "Copy policy",
             "Copied with Catalog → /DPartRoot and each page's /DPart; page ranges are remapped by page number.",
             cls=READONLY)
    if not is_obj(root, Dictionary):
        ctx.info("dpart:root", P, "/DPartRoot", None, cls=READONLY)
        return
    ctx.obj("dpart:/RecordLevel", P, "/RecordLevel", root.get("/RecordLevel"), cls=READONLY)
    ctx.obj("dpart:/NodeNameList", P, "/NodeNameList", root.get("/NodeNameList"), cls=READONLY)
    count = [0]

    def walk(node: Any, label: str, depth: int) -> None:
        if count[0] > 500 or depth > 30 or not is_obj(node, Dictionary):
            return
        count[0] += 1
        start, end = node.get("/Start"), node.get("/End")
        rng = ""
        if start is not None:
            rng = f"pages {ctx.ref_display(start)}" + (f" to {ctx.ref_display(end)}" if end is not None else "")
        ctx.info(f"dpart:{label}", P, f"Node {label}", rng or "(interior node)", cls=READONLY)
        if "/DPM" in node:
            ctx.obj(f"dpart:{label}:/DPM", P, f"Node {label} /DPM", node["/DPM"], cls=READONLY)
        kids = node.get("/DParts")
        if is_obj(kids, Array):
            n = 0
            for arr in kids:
                for kid in arr if is_obj(arr, Array) else [arr]:
                    n += 1
                    walk(kid, f"{label}.{n}", depth + 1)

    walk(root.get("/DPartRootNode"), "1", 0)
    pages_with = sum(1 for p in ctx.pdf.pages if "/DPart" in p.obj)
    ctx.info("dpart:pages", P, "Pages with /DPart", pages_with, cls=READONLY)


def _legacy(ctx: Ctx) -> None:
    P = ["Legacy metadata", "Legacy and historical metadata"]
    info = ctx.pdf.trailer.get("/Info")
    info = info if is_obj(info, Dictionary) else None
    has_xmp = is_obj(ctx.pdf.Root.get("/Metadata"), Stream)
    ctx.info("legacy:info_only", P, "Metadata only in the Info dictionary",
             (bool(info) and not has_xmp) or None, cls=READONLY,
             note="Pre-XMP files keep metadata only in /Info; it is copied as-is.")
    ID = ctx.pdf.trailer.get("/ID")
    ctx.info("legacy:single_id", P, "Single-element /ID",
             True if is_obj(ID, Array) and len(ID) == 1 else None, cls=UNREPRODUCIBLE,
             note="qpdf always writes a two-element /ID; a single-element ID cannot be reproduced.")
    tr_ck = ctx.pdf.trailer.get("/DocChecksum")
    for key in ["/DocChecksum", "/SourceModified", "/GTS_PDFXVersion", "/GTS_PDFXConformance", "/Company",
                "/Manager", "/Category", "/PTEX.Fullbanner", "/AAPL:Keywords"]:
        v = info.get(key) if info is not None else None
        where = "Info dictionary"
        if v is None and key == "/DocChecksum" and tr_ck is not None:
            v, where = tr_ck, "trailer"
        ctx.info(f"legacy:{key}", P, key, f"{brief(v)} (in {where})" if v is not None else None, cls=READONLY,
                 note=f"{LEGACY_INFO.get(key, '')} Copied via the {where} entry.".strip())
    sp = ctx.pdf.Root.get("/SpiderInfo")
    ctx.info("legacy:/SpiderInfo", P, "/SpiderInfo (Web Capture)", brief(sp) if sp is not None else None,
             cls=READONLY, note="Copied via Catalog → /SpiderInfo.")


def _filesystem(ctx: Ctx, filename: str, fs: dict, size: int) -> None:
    P = ["Filesystem & transport", "Outside the PDF"]
    note = "Not stored inside the PDF; changing PDF metadata does not change it, and the output file has its own."
    ctx.info("fs:filename", P, "Uploaded filename", filename, cls=READONLY, note=note)
    ctx.info("fs:size", P, "Uploaded size", f"{size:,} bytes", cls=READONLY)
    ctx.info("fs:content_type", P, "Content-Type sent by browser", fs.get("content_type"), cls=READONLY)
    ctx.info("fs:browser_mtime", P, "Modification time (reported by browser)", fs.get("browser_mtime"),
             cls=READONLY, note="From the browser's File API (lastModified). " + note)
    ctx.info("fs:received", P, "Received by server", fs.get("received"), cls=READONLY)
    unavailable = "Not available: browsers do not transmit this with a file upload."
    for key, label in (("ctime", "Filesystem creation time"), ("atime", "Filesystem access time"),
                       ("xattr", "Extended attributes"), ("apple", "macOS com.apple.metadata"),
                       ("finder", "Finder tags"), ("zone", "Windows Zone.Identifier"),
                       ("email", "Email / transport metadata")):
        ctx.info(f"fs:{key}", P, label, None, cls=READONLY, note=unavailable)
