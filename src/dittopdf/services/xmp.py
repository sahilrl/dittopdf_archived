"""XMP packet parsing and editing.

The whole packet is always preserved: when the user changes nothing, the
output receives the original packet byte for byte. Individual properties are
parsed from whatever namespaces are actually present (not only well-known
ones) into a small value tree::

    {"t": "text", "v": "..."}                 simple value
    {"t": "uri", "v": "..."}                  rdf:resource
    {"t": "alt"|"bag"|"seq", "items": [...]}  arrays (items may carry "lang")
    {"t": "struct", "fields": [{"prefix", "uri", "local", "value"}, ...]}

Edits are applied to the packet with lxml so that unrelated properties,
namespaces and custom schemas (PDF/A extension schemas included) survive.
"""

from __future__ import annotations

import copy
import io
import json
from typing import Any

from lxml import etree

RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
XML_NS = "http://www.w3.org/XML/1998/namespace"
X_NS = "adobe:ns:meta/"
_R = "{%s}" % RDF
DESC, LI, BAG, SEQ, ALT = _R + "Description", _R + "li", _R + "Bag", _R + "Seq", _R + "Alt"
ABOUT, RESOURCE, PARSETYPE = _R + "about", _R + "resource", _R + "parseType"
XML_LANG = "{%s}lang" % XML_NS

# prefix -> (namespace URI, title)
NAMESPACES: dict[str, tuple[str, str]] = {
    "dc": ("http://purl.org/dc/elements/1.1/", "Dublin Core"),
    "xmp": ("http://ns.adobe.com/xap/1.0/", "XMP Basic"),
    "xmpRights": ("http://ns.adobe.com/xap/1.0/rights/", "XMP Rights Management"),
    "xmpMM": ("http://ns.adobe.com/xap/1.0/mm/", "XMP Media Management"),
    "pdf": ("http://ns.adobe.com/pdf/1.3/", "Adobe PDF"),
    "xmpTPg": ("http://ns.adobe.com/xap/1.0/t/pg/", "XMP Paged-Text"),
    "xmpBJ": ("http://ns.adobe.com/xap/1.0/bj/", "XMP Basic Job Ticket"),
    "xmpDM": ("http://ns.adobe.com/xmp/1.0/DynamicMedia/", "XMP Dynamic Media"),
    "pdfaid": ("http://www.aiim.org/pdfa/ns/id/", "PDF/A identification"),
    "pdfaExtension": ("http://www.aiim.org/pdfa/ns/extension/", "PDF/A extension schemas"),
    "pdfaSchema": ("http://www.aiim.org/pdfa/ns/schema#", "PDF/A schema"),
    "pdfaProperty": ("http://www.aiim.org/pdfa/ns/property#", "PDF/A property"),
    "pdfaType": ("http://www.aiim.org/pdfa/ns/type#", "PDF/A value type"),
    "pdfaField": ("http://www.aiim.org/pdfa/ns/field#", "PDF/A field"),
    "pdfxid": ("http://www.npes.org/pdfx/ns/id/", "PDF/X identification"),
    "pdfuaid": ("http://www.aiim.org/pdfua/ns/id/", "PDF/UA identification"),
    "pdfvtid": ("http://www.npes.org/pdfvt/ns/id/", "PDF/VT identification"),
    "pdfx": ("http://ns.adobe.com/pdfx/1.3/", "PDF custom Info mirror (pdfx:)"),
    "exif": ("http://ns.adobe.com/exif/1.0/", "EXIF"),
    "exifEX": ("http://cipa.jp/exif/1.0/", "EXIF 2.3+"),
    "aux": ("http://ns.adobe.com/exif/1.0/aux/", "EXIF auxiliary"),
    "tiff": ("http://ns.adobe.com/tiff/1.0/", "TIFF"),
    "photoshop": ("http://ns.adobe.com/photoshop/1.0/", "Photoshop"),
    "crs": ("http://ns.adobe.com/camera-raw-settings/1.0/", "Camera Raw"),
    "Iptc4xmpCore": ("http://iptc.org/std/Iptc4xmpCore/1.0/xmlns/", "IPTC Core"),
    "Iptc4xmpExt": ("http://iptc.org/std/Iptc4xmpExt/2008-02-29/", "IPTC Extension"),
    "plus": ("http://ns.useplus.org/ldf/xmp/1.0/", "PLUS"),
    "stRef": ("http://ns.adobe.com/xap/1.0/sType/ResourceRef#", "ResourceRef"),
    "stEvt": ("http://ns.adobe.com/xap/1.0/sType/ResourceEvent#", "ResourceEvent"),
    "stVer": ("http://ns.adobe.com/xap/1.0/sType/Version#", "Version"),
    "stJob": ("http://ns.adobe.com/xap/1.0/sType/Job#", "Job"),
    "stDim": ("http://ns.adobe.com/xap/1.0/sType/Dimensions#", "Dimensions"),
    "stFnt": ("http://ns.adobe.com/xap/1.0/sType/Font#", "Font"),
    "xmpG": ("http://ns.adobe.com/xap/1.0/g/", "Colorant"),
    "xmpGImg": ("http://ns.adobe.com/xap/1.0/g/img/", "Thumbnail"),
    "illustrator": ("http://ns.adobe.com/illustrator/1.0/", "Illustrator"),
    "xmpidq": ("http://ns.adobe.com/xmp/Identifier/qual/1.0/", "Identifier qualifier"),
}
URI_TO_PREFIX = {uri: prefix for prefix, (uri, _) in NAMESPACES.items()}

# Standard properties per schema, with their value type, so the UI can show
# which ones are absent. T=text, L=language alternative, B=bag, S=seq, X=structure.
SCHEMA_PROPS: dict[str, list[tuple[str, str]]] = {
    "dc": [("title", "L"), ("creator", "S"), ("subject", "B"), ("description", "L"),
           ("publisher", "B"), ("contributor", "B"), ("date", "S"), ("type", "B"),
           ("format", "T"), ("identifier", "T"), ("source", "T"), ("language", "B"),
           ("relation", "B"), ("coverage", "T"), ("rights", "L")],
    "xmp": [("CreateDate", "T"), ("ModifyDate", "T"), ("MetadataDate", "T"), ("CreatorTool", "T"),
            ("BaseURL", "T"), ("Rating", "T"), ("Label", "T"), ("Nickname", "T"),
            ("Identifier", "B"), ("Advisory", "B"), ("Thumbnails", "X")],
    "xmpRights": [("Marked", "T"), ("Owner", "B"), ("UsageTerms", "L"), ("WebStatement", "T"),
                  ("Certificate", "T")],
    "xmpMM": [("DocumentID", "T"), ("InstanceID", "T"), ("OriginalDocumentID", "T"),
              ("DerivedFrom", "X"), ("History", "X"), ("VersionID", "T"), ("Versions", "X"),
              ("RenditionClass", "T"), ("RenditionParams", "T"), ("Ingredients", "X"),
              ("Pantry", "X"), ("ManageTo", "T"), ("ManageUI", "T"), ("Manager", "T"),
              ("ManagerVariant", "T"), ("ManageFrom", "X")],
    "pdf": [("Producer", "T"), ("Keywords", "T"), ("PDFVersion", "T"), ("Trapped", "T")],
    "xmpTPg": [("NPages", "T"), ("MaxPageSize", "X"), ("Fonts", "X"), ("PlateNames", "S"),
               ("SwatchGroups", "X"), ("Colorants", "X")],
    "xmpBJ": [("JobRef", "X")],
    "xmpDM": [("duration", "X"), ("startTimecode", "X"), ("altTimecode", "X"),
              ("videoFrameRate", "T"), ("audioSampleRate", "T"), ("trackNumber", "T"),
              ("artist", "T"), ("album", "T"), ("genre", "T")],
    "pdfaid": [("part", "T"), ("conformance", "T"), ("amd", "T"), ("rev", "T")],
    "pdfaExtension": [("schemas", "X")],
    "pdfxid": [("GTS_PDFXVersion", "T")],
    "pdfuaid": [("part", "T"), ("rev", "T")],
    "pdfvtid": [("GTS_PDFVTVersion", "T")],
}
# Schemas shown even when the packet does not use them (so absence is visible).
ALWAYS_SHOWN = ["dc", "xmp", "xmpRights", "xmpMM", "pdf", "xmpTPg", "xmpBJ", "xmpDM",
                "pdfaid", "pdfaExtension", "pdfxid", "pdfuaid", "pdfvtid"]

# Info key -> XMP property mirroring it (used to propagate overrides).
INFO_TO_XMP = {
    "/Title": ("dc", "title"),
    "/Author": ("dc", "creator"),
    "/Subject": ("dc", "description"),
    "/Keywords": ("pdf", "Keywords"),
    "/Creator": ("xmp", "CreatorTool"),
    "/Producer": ("pdf", "Producer"),
    "/CreationDate": ("xmp", "CreateDate"),
    "/ModDate": ("xmp", "ModifyDate"),
    "/Trapped": ("pdf", "Trapped"),
}

PADDING = (b" " * 99 + b"\n") * 20


def schema_title(prefix: str, uri: str) -> str:
    known = NAMESPACES.get(prefix)
    if known and known[0] == uri:
        return f"{known[1]} ({prefix}:)"
    p = URI_TO_PREFIX.get(uri)
    if p:
        return f"{NAMESPACES[p][1]} ({p}:)"
    return f"{prefix}: <{uri}>"


def prop_id(uri: str, local: str) -> str:
    return f"xmp:p:{uri}#{local}"


def _parser() -> etree.XMLParser:
    return etree.XMLParser(resolve_entities=False, no_network=True, remove_blank_text=False,
                           huge_tree=False, load_dtd=False)


def _qname(tag: str) -> tuple[str, str]:
    if tag.startswith("{"):
        uri, local = tag[1:].split("}", 1)
        return uri, local
    return "", tag


def _prefix_for(el: etree._Element, uri: str) -> str:
    for p, u in (el.nsmap or {}).items():
        if u == uri and p:
            return p
    return URI_TO_PREFIX.get(uri, "ns")


# --------------------------------------------------------------------------- parsing


def parse(data: bytes) -> dict[str, Any]:
    """Parse an XMP packet into property records. Never raises."""
    result: dict[str, Any] = {"ok": False, "error": None, "props": [], "about": None,
                              "wrapper": b"<?xpacket" in data[:200], "toolkit": None}
    try:
        tree = etree.parse(io.BytesIO(data.strip(b"\x00")), _parser())
    except etree.XMLSyntaxError as e:
        result["error"] = f"XMP is not well-formed XML: {e}"
        return result
    root = tree.getroot()
    if root.tag == "{%s}xmpmeta" % X_NS:
        result["toolkit"] = root.get("{%s}xmptk" % X_NS)
    rdf = root if root.tag == _R + "RDF" else root.find(".//" + _R + "RDF")
    if rdf is None:
        result["error"] = "No rdf:RDF element found in the XMP packet"
        return result
    seen: set[tuple[str, str]] = set()
    for desc in rdf.findall(DESC):
        if result["about"] is None:
            result["about"] = desc.get(ABOUT)
        for attr, val in desc.attrib.items():
            uri, local = _qname(attr)
            if uri in (RDF, XML_NS, "") or (uri, local) in seen:
                continue
            seen.add((uri, local))
            prefix = _prefix_for(desc, uri)
            el = etree.Element("{%s}%s" % (uri, local), nsmap={prefix: uri})
            el.text = val
            result["props"].append(_record(uri, local, prefix, {"t": "text", "v": val}, el, "attribute"))
        for child in desc:
            if not isinstance(child.tag, str):
                continue
            uri, local = _qname(child.tag)
            if (uri, local) in seen:
                continue
            seen.add((uri, local))
            try:
                value = _value(child)
            except Exception as e:  # pragma: no cover - defensive
                value = {"t": "text", "v": f"(unparseable: {e})"}
            result["props"].append(_record(uri, local, child.prefix or _prefix_for(child, uri),
                                           value, child, "element"))
    result["ok"] = True
    return result


def _record(uri: str, local: str, prefix: str, value: dict, el: etree._Element, form: str) -> dict:
    return {"uri": uri, "local": local, "prefix": prefix, "value": value, "form": form,
            "xml": etree.tostring(el, encoding="unicode", with_tail=False)}


def _attr_fields(el: etree._Element) -> list[dict]:
    out = []
    for attr, val in el.attrib.items():
        uri, local = _qname(attr)
        if uri in (RDF, XML_NS, ""):
            continue
        out.append({"uri": uri, "local": local, "prefix": _prefix_for(el, uri),
                    "value": {"t": "text", "v": val}})
    return out


def _child_fields(el: etree._Element) -> list[dict]:
    out = []
    for c in el:
        if not isinstance(c.tag, str):
            continue
        uri, local = _qname(c.tag)
        out.append({"uri": uri, "local": local, "prefix": c.prefix or _prefix_for(c, uri),
                    "value": _value(c)})
    return out


def _value(el: etree._Element) -> dict:
    res = el.get(RESOURCE)
    if res is not None:
        return {"t": "uri", "v": res}
    if el.get(PARSETYPE) == "Resource":
        return {"t": "struct", "fields": _attr_fields(el) + _child_fields(el)}
    kids = [c for c in el if isinstance(c.tag, str)]
    if not kids:
        attrs = _attr_fields(el)
        if attrs:
            return {"t": "struct", "fields": attrs}
        v: dict = {"t": "text", "v": el.text or ""}
        if el.get(XML_LANG):
            v["lang"] = el.get(XML_LANG)
        return v
    first = kids[0]
    if first.tag in (BAG, SEQ, ALT):
        items = []
        for li in first:
            if not isinstance(li.tag, str) or li.tag != LI:
                continue
            item = _value(li)
            if li.get(XML_LANG):
                item["lang"] = li.get(XML_LANG)
            items.append(item)
        return {"t": {BAG: "bag", SEQ: "seq", ALT: "alt"}[first.tag], "items": items}
    if first.tag == DESC:
        return {"t": "struct", "fields": _attr_fields(first) + _child_fields(first)}
    return {"t": "struct", "fields": _child_fields(el)}


# --------------------------------------------------------------------------- presentation


def display(value: dict, indent: int = 0) -> str:
    pad = "  " * indent
    t = value.get("t")
    if t in ("text", "uri"):
        lang = f"[{value['lang']}] " if value.get("lang") and value["lang"] != "x-default" else ""
        return lang + value.get("v", "")
    if t in ("alt", "bag", "seq"):
        items = value.get("items", [])
        if not items:
            return f"(empty {t})"
        simple = all(i.get("t") in ("text", "uri") for i in items)
        if simple:
            parts = [display(i) for i in items]
            return "; ".join(parts) if t == "alt" else "\n".join(f"{pad}• {p}" for p in parts)
        lines = []
        for n, item in enumerate(items, 1):
            lines.append(f"{pad}[{n}]")
            lines.append(display(item, indent + 1))
        return "\n".join(lines)
    if t == "struct":
        lines = []
        for f in value.get("fields", []):
            inner = display(f["value"], indent + 1)
            if "\n" in inner or f["value"].get("t") in ("struct", "bag", "seq"):
                lines.append(f"{pad}{f['prefix']}:{f['local']}:")
                lines.append(inner if inner.startswith(pad + "  ") else "  " * (indent + 1) + inner)
            else:
                lines.append(f"{pad}{f['prefix']}:{f['local']} = {inner}")
        return "\n".join(lines) if lines else f"{pad}(empty structure)"
    return json.dumps(value)


def value_kind(value: dict) -> str:
    """'xmp-text' (single value or language alternative), 'xmp-list' or 'xmp-struct'."""
    t = value.get("t")
    if t in ("text", "uri"):
        return "xmp-text"
    items = value.get("items", [])
    simple = all(i.get("t") == "text" for i in items)
    if t == "alt" and simple:
        return "xmp-text"
    if t in ("bag", "seq") and simple:
        return "xmp-list"
    return "xmp-struct"


def edit_value(value: dict) -> str | None:
    kind = value_kind(value)
    t = value.get("t")
    if kind == "xmp-text":
        if t == "alt":
            items = value.get("items", [])
            default = next((i for i in items if i.get("lang") == "x-default"), items[0] if items else None)
            return default.get("v", "") if default else ""
        return value.get("v", "")
    if kind == "xmp-list":
        return "\n".join(i.get("v", "") for i in value.get("items", []))
    return None


def canonical(value: dict) -> str:
    return "x:" + json.dumps(value, sort_keys=True, ensure_ascii=False)


# --------------------------------------------------------------------------- editing


class XmpEditError(ValueError):
    pass


def check_wellformed(text: str) -> str | None:
    """Return an error message if ``text`` is not a usable XMP packet."""
    try:
        tree = etree.parse(io.BytesIO(text.encode("utf-8")), _parser())
    except etree.XMLSyntaxError as e:
        return f"not well-formed XML: {e}"
    root = tree.getroot()
    if root.tag != _R + "RDF" and root.find(".//" + _R + "RDF") is None:
        return "no rdf:RDF element"
    return None


def apply_edits(packet: bytes, edits: list[dict]) -> bytes:
    """Apply property edits to ``packet``.

    Each edit is ``{"uri", "local", "prefix", "op", ...}`` where ``op`` is

    * ``"text"`` with ``value`` (and ``vtype`` T/L for creation): set a simple
      value or the x-default item of a language alternative;
    * ``"list"`` with ``value`` (list of strings) and ``vtype`` B/S;
    * ``"xml"`` with ``xml``: replace with a serialized property element
      (used to take a property, including structures, from another packet);
    * ``"remove"``.
    """
    if not edits:
        return packet
    tree = etree.parse(io.BytesIO(packet.strip(b"\x00")), _parser())
    root = tree.getroot()
    rdf = root if root.tag == _R + "RDF" else root.find(".//" + _R + "RDF")
    if rdf is None:
        raise XmpEditError("no rdf:RDF element")
    for e in edits:
        _apply_one(rdf, e)
    body = etree.tostring(tree, encoding="utf-8", xml_declaration=False)
    end = body.rfind(b"<?xpacket end")
    if end != -1:
        body = body[:end].rstrip() + b"\n" + PADDING + body[end:]
    return body


def _find(rdf: etree._Element, uri: str, local: str):
    qn = "{%s}%s" % (uri, local)
    for desc in rdf.findall(DESC):
        if qn in desc.attrib:
            return desc, None
        el = desc.find(qn)
        if el is not None:
            return desc, el
    return None, None


def _target_desc(rdf: etree._Element, uri: str, prefix: str) -> etree._Element:
    descs = rdf.findall(DESC)
    for d in descs:
        if uri in (d.nsmap or {}).values():
            return d
    about = descs[0].get(ABOUT, "") if descs else ""
    d = etree.SubElement(rdf, DESC, nsmap={prefix: uri, "rdf": RDF})
    d.set(ABOUT, about)
    return d


def _apply_one(rdf: etree._Element, e: dict) -> None:
    uri, local, prefix, op = e["uri"], e["local"], e.get("prefix") or URI_TO_PREFIX.get(e["uri"], "ns"), e["op"]
    qn = "{%s}%s" % (uri, local)
    desc, el = _find(rdf, uri, local)
    if op == "remove":
        if desc is not None and el is None:
            del desc.attrib[qn]
        elif el is not None:
            el.getparent().remove(el)
        return
    if op == "xml":
        new = etree.fromstring(e["xml"].encode("utf-8"), _parser())
        if desc is not None and el is None:
            del desc.attrib[qn]
        if el is not None:
            el.getparent().replace(el, copy.deepcopy(new))
        else:
            _target_desc(rdf, uri, prefix).append(copy.deepcopy(new))
        return
    if op == "text":
        value = e["value"]
        if desc is not None and el is None:
            desc.set(qn, value)
            return
        if el is None:
            el = etree.SubElement(_target_desc(rdf, uri, prefix), qn)
            if e.get("vtype") == "L":
                alt = etree.SubElement(el, ALT)
                li = etree.SubElement(alt, LI)
                li.set(XML_LANG, "x-default")
                li.text = value
            else:
                el.text = value
            return
        alt = el.find(ALT)
        if alt is not None:
            li = next((x for x in alt.findall(LI) if x.get(XML_LANG) == "x-default"), None)
            if li is None:
                li = alt.find(LI)
            if li is None:
                li = etree.SubElement(alt, LI)
                li.set(XML_LANG, "x-default")
            li.text = value
            return
        for c in list(el):
            el.remove(c)
        el.attrib.pop(RESOURCE, None)
        el.text = value
        return
    if op == "list":
        if desc is not None and el is None:
            del desc.attrib[qn]
            el = None
        if el is None:
            el = etree.SubElement(_target_desc(rdf, uri, prefix), qn)
            container_tag = SEQ if e.get("vtype") == "S" else BAG
        else:
            existing = next((c for c in el if c.tag in (BAG, SEQ, ALT)), None)
            container_tag = existing.tag if existing is not None else (SEQ if e.get("vtype") == "S" else BAG)
            for c in list(el):
                el.remove(c)
            el.text = None
        container = etree.SubElement(el, container_tag)
        for item in e["value"]:
            li = etree.SubElement(container, LI)
            li.text = item
        return
    raise XmpEditError(f"unknown XMP edit {op!r}")


def minimal_packet() -> bytes:
    return (
        b'<?xpacket begin="\xef\xbb\xbf" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
        b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="' + RDF.encode() + b'">'
        b'<rdf:Description rdf:about=""/></rdf:RDF></x:xmpmeta>\n' + PADDING + b'<?xpacket end="w"?>'
    )
