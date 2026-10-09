"""Decide whether two image XObjects show the same picture, however they are encoded.

Another producer often re-encodes the same logo: a new JPEG of it, Flate instead of a
predictor, DeviceRGB instead of CalRGB. The stored bytes then differ while the
picture doesn't. Both images are decoded and compared pixel by pixel (transparency
included):

* the **mean** difference stays tiny for re-encoding noise;
* the **block** difference (the largest difference between 4×4-pixel block averages)
  averages that noise out but keeps real changes: a digit, a barcode bar or a chart
  bar that differs shows up even when it barely moves the mean.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pikepdf import Array, PdfImage, Stream

from dittopdf.services.pdfobj import is_obj

MAX_PIXELS = 16_000_000
MEAN_LIMIT = 4.0    # average difference per channel, out of 255
BLOCK_LIMIT = 32    # largest difference between 4×4 block averages, out of 255
BLOCK = 4


@dataclass
class Match:
    identical: bool
    mean: float
    block: int

    def describe(self) -> str:
        if self.identical:
            return "identical pixels"
        return f"mean difference {self.mean:.1f}/255, largest 4×4-block difference {self.block}/255"


def picture(img: Any) -> tuple[Any, Any, Any] | None:
    """(RGB image, alpha image or None, mask kind) or None if it can't be decoded."""
    try:
        if not is_obj(img, Stream) or img.get("/ImageMask") or \
                int(img.get("/Width", 0)) * int(img.get("/Height", 0)) > MAX_PIXELS:
            return None
        rgb = PdfImage(img).as_pil_image().convert("RGB")
        alpha, kind = None, None
        smask, mask = img.get("/SMask"), img.get("/Mask")
        if is_obj(smask, Stream):
            alpha, kind = PdfImage(smask).as_pil_image().convert("L"), "smask"
        elif is_obj(mask, Stream):
            alpha, kind = PdfImage(mask).as_pil_image().convert("L"), "stencil"
        elif is_obj(mask, Array):
            kind = ("colour key", tuple(int(x) for x in mask))
        if alpha is not None and alpha.size != rgb.size:
            alpha = alpha.resize(rgb.size)
        return rgb, alpha, kind
    except Exception:
        return None


def compare(a: tuple[Any, Any, Any] | None, b: tuple[Any, Any, Any] | None) -> Match | None:
    """How close two decoded pictures are; None if they can't be the same picture."""
    from PIL import Image, ImageChops, ImageStat

    if a is None or b is None or a[0].size != b[0].size or a[2] != b[2]:
        return None
    pairs = [(a[0], b[0])] + ([(a[1], b[1])] if a[1] is not None else [])
    if all(x.tobytes() == y.tobytes() for x, y in pairs):
        return Match(True, 0.0, 0)
    if isinstance(a[2], tuple):
        return None  # a colour key applies to exact sample values: any difference changes the transparency
    mean, block = 0.0, 0
    for x, y in pairs:
        means = ImageStat.Stat(ImageChops.difference(x, y)).mean
        mean = max(mean, sum(means) / len(means))
        size = (max(1, x.width // BLOCK), max(1, x.height // BLOCK))
        d = ImageChops.difference(x.resize(size, Image.BOX), y.resize(size, Image.BOX))
        ext = d.getextrema()
        block = max(block, max(hi for _, hi in ext) if isinstance(ext[0], tuple) else ext[1])
    return Match(False, mean, block)


def same_picture(m: Match | None, mode: str) -> bool:
    """``mode``: "identical" (pixels equal) or "similar" (within the re-encoding limits)."""
    if m is None:
        return False
    return m.identical or (mode == "similar" and m.mean <= MEAN_LIMIT and m.block <= BLOCK_LIMIT)
