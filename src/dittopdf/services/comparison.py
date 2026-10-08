"""Match two inspections entry by entry and decide what can be copied."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from dittopdf.services.model import (
    CLASSES, COPY, CUSTOM, DIRECT, KEEP, READONLY, RECONSTRUCT, REGENERATE, REMOVE, UNREPRODUCIBLE,
)

STATUS = {
    "same": "Same",
    "different": "Different",
    "only_original": "Only in original",
    "only_second": "Only in second PDF",
    "absent": "Absent in both",
    "unable_inspect": "Unable to inspect",
}
COPY_STATUS = {
    "copy": "Copy",
    "opt_in": "Kept (copy is opt-in)",
    "option": "Set by output option",
    "unable_copy": "Unable to copy",
    "readonly": "Read-only",
    "regenerated": "Regenerated",
}

PAGE_RE = re.compile(r"^page:(\d+):")
ANNOT_RE = re.compile(r"^annot:(\d+):(\d+):")
IMAGE_MD_RE = re.compile(r"^image:(\d+):/Metadata$")
SIDE_KEYS = ("present", "display", "detail", "edit", "error", "canon")


def _side(e: dict | None) -> dict:
    if e is None:
        return {"present": False, "display": "", "detail": None, "edit": None, "error": None, "canon": None}
    return {k: e.get(k) for k in SIDE_KEYS}


def _status(o: dict | None, s: dict | None) -> str:
    if (o and o.get("error")) or (s and s.get("error")):
        return "unable_inspect"
    op, sp = bool(o and o["present"]), bool(s and s["present"])
    if not op and not sp:
        return "absent"
    if op and not sp:
        return "only_original"
    if sp and not op:
        return "only_second"
    return "same" if o["canon"] == s["canon"] else "different"


def merged_order(a: list[dict], b: list[dict]) -> list[str]:
    """Ids of ``a`` in order, with ids only in ``b`` placed after their group in ``a``."""
    order = [e["id"] for e in a]
    known = set(order)
    last_in_path: dict[tuple, int] = {}
    last_in_section: dict[str, int] = {}
    for i, e in enumerate(a):
        last_in_path[tuple(e["path"])] = i
        last_in_section[e["path"][0]] = i
    inserts: dict[int, list[str]] = {}
    tail: list[str] = []
    for e in b:
        if e["id"] in known:
            continue
        at = last_in_path.get(tuple(e["path"]), last_in_section.get(e["path"][0]))
        if at is None:
            tail.append(e["id"])
        else:
            inserts.setdefault(at, []).append(e["id"])
    out = []
    for i, id in enumerate(order):
        out.append(id)
        out.extend(inserts.get(i, []))
    return out + tail


def compare(orig: dict, second: dict) -> dict[str, Any]:
    o_map = {e["id"]: e for e in orig["entries"]}
    s_map = {e["id"]: e for e in second["entries"]}
    o_pages, s_pages = orig["summary"]["pages"], second["summary"]["pages"]
    rows = []
    for id in merged_order(orig["entries"], second["entries"]):
        o, s = o_map.get(id), s_map.get(id)
        spec = o if o is not None and (o["present"] or s is None or not s["present"]) else s
        row = {
            "id": id,
            "path": spec["path"],
            "label": spec["label"],
            "cls": spec["cls"],
            "cls_label": CLASSES[spec["cls"]],
            "kind": (o or s)["kind"] if (o and o["present"]) or not s else s["kind"],
            "editable": spec["editable"],
            "actions": list(spec["actions"]),
            "default": spec["default"],
            "note": spec["note"] or (o or {}).get("note") or "",
            "warn": " ".join(x for x in {(o or {}).get("warn", ""), (s or {}).get("warn", "")} if x),
            "choices": spec.get("choices"),
            "xmp": (o or {}).get("xmp") or (s or {}).get("xmp"),
            "xml_o": ((o or {}).get("xmp") or {}).get("xml") if o and o["present"] else None,
            "xml_s": ((s or {}).get("xmp") or {}).get("xml") if s and s["present"] else None,
            "o": _side(o),
            "s": _side(s),
        }
        row["status"] = _status(o, s)
        _feasibility(row, o_map, s_map, o_pages, s_pages)
        rows.append(row)
    return {"rows": rows, "tree": build_tree(rows), "counts": _counts(rows),
            "warnings": _warnings(orig, second)}


def _feasibility(row: dict, o_map: dict, s_map: dict, o_pages: int, s_pages: int) -> None:
    id, cls = row["id"], row["cls"]
    reason = ""
    if cls == READONLY:
        row["copy_status"] = "readonly"
    elif cls == REGENERATE:
        row["copy_status"] = "regenerated"
    else:
        row["copy_status"] = "copy" if row["default"] == COPY else ("opt_in" if row["actions"] else "option")
        if not row["actions"] and cls == UNREPRODUCIBLE:
            row["copy_status"] = "option" if row["id"].startswith(("catalog:/StructTreeRoot", "page:", "annot:",
                                                                     "catalog:/AcroForm")) else "unable_copy"
    m = PAGE_RE.match(id)
    if m and row["actions"]:
        n = int(m.group(1))
        if n > s_pages:
            reason = f"The second PDF has only {s_pages} page(s); page {n} does not exist there."
        elif n > o_pages:
            reason = f"The original has no page {n}; the second PDF's value is kept."
    m = ANNOT_RE.match(id)
    if m and row["actions"]:
        p, j = m.group(1), m.group(2)
        o_sub = o_map.get(f"annot:{p}:{j}:/Subtype")
        s_sub = s_map.get(f"annot:{p}:{j}:/Subtype")
        if not (o_sub and o_sub["present"]):
            reason = "The original has no annotation at this position."
        elif not (s_sub and s_sub["present"]):
            reason = ("The second PDF has no annotation at this position. Use 'Replace annotations' or "
                      "'Add original's annotations' in the output options to copy whole annotations.")
        elif o_sub["display"] != s_sub["display"]:
            reason = (f"The annotation at this position in the second PDF is {s_sub['display']}, not "
                      f"{o_sub['display']}.")
    m = IMAGE_MD_RE.match(id)
    if m and row["actions"]:
        n = m.group(1)
        o_sha, s_sha = o_map.get(f"image:{n}:sha"), s_map.get(f"image:{n}:sha")
        if not (o_sha and s_sha and o_sha["present"] and s_sha["present"] and o_sha["display"] == s_sha["display"]):
            reason = "Image XMP is only copied to an identical image; image {} differs or is missing.".format(n)
    if reason:
        row["copy_status"] = "unable_copy"
        row["copy_reason"] = reason
        target_exists = "does not exist" not in reason and "no annotation" not in reason
        row["actions"] = [a for a in (KEEP, REMOVE, CUSTOM) if a in row["actions"]] if target_exists else []
        row["default"] = KEEP if row["actions"] else ""
    else:
        row["copy_reason"] = ""


def build_tree(rows: list[dict]) -> list[dict]:
    sections: dict[str, dict] = {}
    for r in rows:
        sec = sections.setdefault(r["path"][0], {"name": r["path"][0], "groups": {}, "counts": Counter()})
        title = r["path"][1] if len(r["path"]) > 1 else ""
        grp = sec["groups"].setdefault(title, {"title": title, "rows": [], "counts": Counter()})
        r["sub"] = " › ".join(r["path"][2:])
        grp["rows"].append(r)
        grp["counts"][r["status"]] += 1
        sec["counts"][r["status"]] += 1
    out = []
    for sec in sections.values():
        sec["groups"] = list(sec["groups"].values())
        out.append(sec)
    return out


def _counts(rows: list[dict]) -> dict:
    c: Counter = Counter(r["status"] for r in rows)
    return dict(c)


def _warnings(orig: dict, second: dict) -> list[str]:
    out = []
    o, s = orig["summary"], second["summary"]
    if o["pages"] != s["pages"]:
        out.append(f"The original has {o['pages']} page(s) and the second PDF has {s['pages']}. Page-level "
                   "properties are matched by page number; references to pages that do not exist in the second "
                   "PDF are dropped.")
    if s.get("signed"):
        out.append("The second PDF is digitally signed. Writing a new file invalidates its signatures.")
    if o.get("signed"):
        out.append("The original is digitally signed. Its signatures are shown for diagnosis only and are not "
                   "copied (a signature cannot be moved to another document).")
    if s.get("encrypted"):
        out.append("The second PDF is encrypted. Choose how the output is encrypted in the output options.")
    if o["sha256"] == s["sha256"]:
        out.append("Both uploads are byte-for-byte identical.")
    return out


def counts_by_section(tree: list[dict]) -> dict[str, dict]:
    return {sec["name"]: dict(sec["counts"]) for sec in tree}


__all__ = ["compare", "STATUS", "COPY_STATUS", "DIRECT", "RECONSTRUCT"]
