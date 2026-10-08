"""The inspection data model: a flat, ordered list of *entries*.

An entry describes one property of one PDF. Its ``id`` is stable across files
(``info:/Title``, ``page:3:/MediaBox``, ``xmp:p:<uri>#title``…) so the same
property can be matched between the original, the second PDF and the output.
``path`` gives its place in the UI hierarchy: ``[section, group, subgroup?]``.

Every entry carries a copy classification (see :data:`CLASSES`) describing the
technically correct way to reproduce it.
"""

from __future__ import annotations

from typing import Any

from dittopdf.services import pdfobj

DIRECT = "direct"
RECONSTRUCT = "reconstruct"
READONLY = "readonly"
REGENERATE = "regenerate"
UNREPRODUCIBLE = "unreproducible"

CLASSES = {
    DIRECT: "Directly copyable",
    RECONSTRUCT: "Copyable with reconstruction",
    READONLY: "Read-only / diagnostic",
    REGENERATE: "Must be regenerated",
    UNREPRODUCIBLE: "Cannot reliably be reproduced",
}

# Actions the edit screen offers for an entry.
COPY, KEEP, REMOVE, CUSTOM = "copy", "keep", "remove", "custom"
ACTIONS = {
    COPY: "Copy original",
    KEEP: "Keep second PDF's",
    REMOVE: "Remove",
    CUSTOM: "Custom value",
}

# Value kinds that the edit screen can edit, and the control it uses for each.
EDITABLE_KINDS = {"text", "date", "name", "number", "bool", "binary", "object", "xml",
                  "xmp-text", "xmp-list"}


def entry(
    id: str,
    path: list[str],
    label: str,
    *,
    present: bool = True,
    display: str = "",
    detail: str | None = None,
    kind: str = "info",
    edit: str | None = None,
    canon: str | None = None,
    cls: str = READONLY,
    editable: bool = False,
    actions: list[str] | None = None,
    default: str | None = None,
    note: str = "",
    error: str | None = None,
    choices: list[str] | None = None,
    warn: str = "",
) -> dict[str, Any]:
    if actions is None:
        actions = [COPY, KEEP, REMOVE] if cls in (DIRECT, RECONSTRUCT) else []
        if editable and actions:
            actions.append(CUSTOM)
    if default is None:
        default = COPY if actions and cls in (DIRECT, RECONSTRUCT) else (KEEP if actions else "")
    if canon is None and present and error is None:
        canon = "v:" + display
    return {
        "id": id,
        "path": path,
        "label": label,
        "present": present,
        "display": display,
        "detail": detail,
        "kind": kind,
        "edit": edit,
        "canon": canon,
        "cls": cls,
        "editable": editable,
        "actions": actions,
        "default": default,
        "note": note,
        "error": error,
        "choices": choices,
        "warn": warn,
    }


def obj_entry(
    id: str,
    path: list[str],
    label: str,
    obj: Any,
    *,
    cls: str = READONLY,
    editable: bool | None = None,
    pages: dict | None = None,
    missing: bool = False,
    **kw: Any,
) -> dict[str, Any]:
    """Build an entry from a pikepdf value. ``missing=True`` (or obj None) marks it absent."""
    absent_kind = kw.pop("absent_kind", None)
    kind = kw.pop("kind", None)
    if missing or obj is None:
        kind = kind or absent_kind or "info"
        return entry(id, path, label, present=False, display="", cls=cls, kind=kind,
                     editable=bool(editable) and kind in EDITABLE_KINDS, canon=None, **kw)
    kind = kind or pdfobj.kind_of(obj)
    try:
        display = pdfobj.brief(obj, pages)
        det = pdfobj.detail(obj, pages=pages)
        canon = pdfobj.canonical(obj, pages)
        edit = edit_value(obj, kind)
    except Exception as e:  # malformed object
        return entry(id, path, label, present=True, display="", cls=cls, error=f"Unable to read: {e}", **kw)
    if editable is None:
        editable = cls in (DIRECT, RECONSTRUCT) and kind in EDITABLE_KINDS
    if kind == "date":
        iso = pdfobj.pdf_date_to_iso(display)
        if iso:
            display = f"{display}  ({iso})"
    return entry(id, path, label, present=True, display=display, detail=det, kind=kind,
                 edit=edit, canon=canon, cls=cls, editable=bool(editable) and kind in EDITABLE_KINDS,
                 **kw)


def edit_value(obj: Any, kind: str) -> str | None:
    """The string the edit screen pre-fills for ``obj``."""
    import json

    if kind in ("text", "date"):
        return pdfobj.decode_text(bytes(obj))
    if kind == "binary":
        return bytes(obj).hex()
    if kind == "name":
        return str(obj)
    if kind == "number":
        return str(obj)
    if kind == "bool":
        return "true" if obj else "false"
    if kind == "object":
        return json.dumps(pdfobj.to_json(obj), indent=1, ensure_ascii=False)
    return None


def info_entry(id: str, path: list[str], label: str, value: Any, *, note: str = "",
               cls: str = READONLY, error: str | None = None, warn: str = "") -> dict[str, Any]:
    """A diagnostic entry with a plain Python value."""
    if error:
        return entry(id, path, label, present=True, display="", cls=cls, note=note, error=error, actions=[])
    if value is None or value == "":
        return entry(id, path, label, present=False, cls=cls, note=note, warn=warn, actions=[])
    if isinstance(value, bool):
        display = "Yes" if value else "No"
    else:
        display = str(value)
    return entry(id, path, label, display=display, cls=cls, note=note, warn=warn, actions=[])
