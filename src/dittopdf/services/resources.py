"""Walk page resources to find fonts and images, and where/how big images are drawn.

Fonts and images may be reached from page resources, nested form XObjects,
annotation appearance streams and Type 3 fonts; all of those are visited.
Image resolution is derived by interpreting the content streams' ``q``/``Q``/
``cm``/``Do`` operators to find the size at which each image is placed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import pikepdf
from pikepdf import Dictionary, Name, Stream

from dittopdf.services.pdfobj import is_obj

Matrix = tuple[float, float, float, float, float, float]
IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
MAX_OPS = 400_000  # per document, to bound time on huge content streams


def mul(m: Matrix, n: Matrix) -> Matrix:
    """m × n (apply m, then n)."""
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return (a * A + b * C, a * B + b * D, c * A + d * C, c * B + d * D,
            e * A + f * C + E, e * B + f * D + F)


@dataclass
class Found:
    obj: Any
    pages: set[int] = field(default_factory=set)
    names: set[str] = field(default_factory=set)
    via: set[str] = field(default_factory=set)
    placements: list[dict] = field(default_factory=list)


@dataclass
class Resources:
    fonts: dict[Any, Found] = field(default_factory=dict)
    images: dict[Any, Found] = field(default_factory=dict)
    forms_pieceinfo: list[tuple[str, Any]] = field(default_factory=list)
    inline_images: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    ops_budget_hit: bool = False


def _key(obj: Any) -> Any:
    return obj.objgen if obj.is_indirect else id(obj)


def collect(pdf: pikepdf.Pdf, max_pages: int | None = None) -> Resources:
    res = Resources()
    budget = [MAX_OPS]
    for i, page in enumerate(pdf.pages, 1):
        if max_pages is not None and i > max_pages:
            break
        try:
            uu = float(page.obj.get("/UserUnit", 1) or 1)
            resources = page.obj.get("/Resources")
            if resources is None:
                resources = _inherited(page.obj, "/Resources")
            _scan_resources(resources, i, "page", res, set())
            _scan_content(page, resources, i, res, IDENTITY, uu, budget, 0, set())
            for annot in page.obj.get("/Annots", []) or []:
                ap = annot.get("/AP") if is_obj(annot, Dictionary) else None
                if not is_obj(ap, Dictionary):
                    continue
                for state in ap.values():
                    streams = [state] if is_obj(state, Stream) else (
                        list(state.values()) if is_obj(state, Dictionary) else [])
                    for s in streams:
                        if is_obj(s, Stream):
                            _scan_resources(s.get("/Resources"), i, "annotation appearance", res, set())
        except Exception as e:
            res.errors.append(f"page {i}: {e}")
    res.ops_budget_hit = budget[0] <= 0
    return res


def _inherited(page: Any, key: str) -> Any:
    node, hops = page, 0
    while node is not None and hops < 50:
        if key in node:
            return node[key]
        node = node.get("/Parent")
        hops += 1
    return None


def _scan_resources(resources: Any, page: int, via: str, res: Resources, seen: set) -> None:
    if not is_obj(resources, Dictionary):
        return
    fonts = resources.get("/Font")
    if is_obj(fonts, Dictionary):
        for name, font in fonts.items():
            if not is_obj(font, Dictionary):
                continue
            f = res.fonts.setdefault(_key(font), Found(font))
            f.pages.add(page)
            f.names.add(str(name))
            f.via.add(via)
            if font.get("/Subtype") == Name.Type3:
                k = ("t3", _key(font))
                if k not in seen:
                    seen.add(k)
                    _scan_resources(font.get("/Resources"), page, "Type 3 glyph", res, seen)
    xobjs = resources.get("/XObject")
    if is_obj(xobjs, Dictionary):
        for name, x in xobjs.items():
            if not is_obj(x, Stream):
                continue
            sub = x.get("/Subtype")
            if sub == Name.Image:
                f = res.images.setdefault(_key(x), Found(x))
                f.pages.add(page)
                f.names.add(str(name))
                f.via.add(via)
                for mk in ("/SMask", "/Mask"):
                    m = x.get(mk)
                    if is_obj(m, Stream):
                        mf = res.images.setdefault(_key(m), Found(m))
                        mf.pages.add(page)
                        mf.via.add(f"{mk[1:]} of {name}")
            elif sub == Name.Form:
                k = _key(x)
                if k in seen:
                    continue
                seen.add(k)
                if "/PieceInfo" in x:
                    res.forms_pieceinfo.append((f"form XObject {name} (page {page})", x["/PieceInfo"]))
                _scan_resources(x.get("/Resources"), page, f"form XObject {name}", res, seen)


def _scan_content(content: Any, resources: Any, page: int, res: Resources, ctm: Matrix,
                  user_unit: float, budget: list[int], depth: int, stack: set) -> None:
    if depth > 12 or budget[0] <= 0:
        return
    try:
        ops = pikepdf.parse_content_stream(content)
    except Exception as e:
        res.errors.append(f"page {page}: content stream not parseable for resolution ({e})")
        return
    xobjs = resources.get("/XObject") if is_obj(resources, Dictionary) else None
    gstack: list[Matrix] = []
    cur = ctm
    for item in ops:
        budget[0] -= 1
        if budget[0] <= 0:
            return
        if isinstance(item, pikepdf.ContentStreamInlineImage):
            iimg = item.iimage
            res.inline_images.append({"page": page, "width": iimg.width, "height": iimg.height,
                                      **_placement(cur, iimg.width, iimg.height, user_unit)})
            continue
        operands, op = item.operands, str(item.operator)
        if op == "q":
            gstack.append(cur)
        elif op == "Q":
            cur = gstack.pop() if gstack else ctm
        elif op == "cm" and len(operands) == 6:
            try:
                cur = mul(tuple(float(v) for v in operands), cur)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                pass
        elif op == "Do" and operands and is_obj(xobjs, Dictionary):
            x = xobjs.get(str(operands[0]))
            if not is_obj(x, Stream):
                continue
            if x.get("/Subtype") == Name.Image:
                f = res.images.setdefault(_key(x), Found(x))
                w, h = int(x.get("/Width", 0)), int(x.get("/Height", 0))
                f.placements.append({"page": page, **_placement(cur, w, h, user_unit)})
            elif x.get("/Subtype") == Name.Form and _key(x) not in stack:
                m = x.get("/Matrix")
                fm = tuple(float(v) for v in m) if m is not None and len(m) == 6 else IDENTITY
                _scan_content(x, x.get("/Resources") or resources, page, res, mul(fm, cur),  # type: ignore[arg-type]
                              user_unit, budget, depth + 1, stack | {_key(x)})


def _placement(m: Matrix, w: int, h: int, user_unit: float) -> dict:
    a, b, c, d, _, _ = m
    wpt = math.hypot(a, b) * user_unit
    hpt = math.hypot(c, d) * user_unit
    out = {"width_pt": round(wpt, 2), "height_pt": round(hpt, 2)}
    if wpt > 0 and hpt > 0 and w and h:
        out["dpi_x"] = round(w / (wpt / 72), 1)
        out["dpi_y"] = round(h / (hpt / 72), 1)
    return out
