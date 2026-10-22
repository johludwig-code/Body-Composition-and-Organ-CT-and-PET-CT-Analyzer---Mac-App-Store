#!/usr/bin/env python3
"""Make the app icon set from the source artwork (OPEN_QUESTIONS #10).

    build/runtime/python/bin/python3 -I Scripts/make_app_icon.py [source.png]

The source, `App/Icon/AppIcon-source.png`, is the owner's artwork of
22 October 2026: a rounded square on a white page with a soft shadow below
it, and corners rounder than the Mac's own icon shape. A Mac icon is a
1024-pixel canvas with an 824-pixel rounded square in the middle, transparent
around it; macOS 26 puts an icon of another shape on a grey plate of its
own. So the script cuts the square out of the page, fills its rounder
corners with the colours next to them, sets it into the Mac's shape with a
shadow of its own, and writes every size the asset catalog lists. Pillow
and NumPy come from the bundled runtime; nothing is installed for this.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "App" / "Icon" / "AppIcon-source.png"
ICONSET = ROOT / "App" / "Resources" / "Assets.xcassets" / "AppIcon.appiconset"

CANVAS = 1024
BODY = 824
# Apple's icon corners are continuous; a superellipse of exponent 5 is the
# usual stand-in for them.
EXPONENT = 5.0
# The page is white; the shadow under the artwork never gets darker than
# this, and the artwork's own pixels are all darker.
PAGE_LUMINANCE = 215


def body_box(rgb: np.ndarray) -> tuple[int, int, int, int]:
    """The square the artwork fills, from its darker-than-the-page pixels."""
    lum = rgb.mean(axis=2)
    ys, xs = np.nonzero(lum < PAGE_LUMINANCE)
    left, right, top, bottom = xs.min(), xs.max(), ys.min(), ys.max()
    side = min(right - left, bottom - top) + 1
    return int(left), int(top), int(left + side), int(top + side)


def page_mask(rgb: np.ndarray) -> np.ndarray:
    """True where the white page shows: light pixels joined to a corner.
    Light pixels inside the artwork (the glowing nodes) are not joined."""
    light = rgb.mean(axis=2) >= PAGE_LUMINANCE
    page = np.zeros_like(light)
    h, w = light.shape
    stack = [(y, x) for y, x in ((0, 0), (0, w - 1), (h - 1, 0), (h - 1, w - 1)) if light[y, x]]
    while stack:
        y, x = stack.pop()
        if page[y, x] or not light[y, x]:
            continue
        page[y, x] = True
        if y > 0:
            stack.append((y - 1, x))
        if y < h - 1:
            stack.append((y + 1, x))
        if x > 0:
            stack.append((y, x - 1))
        if x < w - 1:
            stack.append((y, x + 1))
    return page


# The artwork's own edge is antialiased against the white page; seeding the
# fill from it drew light streaks into the corners, so the fill starts this
# many pixels inside the edge.
EDGE_PX = 10


def grow(mask: np.ndarray, steps: int) -> np.ndarray:
    for _ in range(steps):
        mask = (
            mask
            | np.roll(mask, 1, 0) | np.roll(mask, -1, 0)
            | np.roll(mask, 1, 1) | np.roll(mask, -1, 1)
        )
    return mask


def fill(rgb: np.ndarray, page: np.ndarray) -> np.ndarray:
    """Fills the page in the corners, and the artwork's edge beside it, with
    the colours inside, growing outward one pixel ring at a time, and softens
    what it filled so that no streak shows."""
    hole = grow(page, EDGE_PX)
    out = rgb.astype(np.float64)
    known = ~hole
    while not known.all():
        total = np.zeros_like(out)
        count = np.zeros(known.shape)
        for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1)):
            shifted = np.roll(np.roll(known, dy, 0), dx, 1)
            values = np.roll(np.roll(out, dy, 0), dx, 1)
            total += values * shifted[..., None]
            count += shifted
        ring = ~known & (count > 0)
        out[ring] = total[ring] / count[ring][:, None]
        known = known | ring
    blurred = np.asarray(
        Image.fromarray(out.astype(np.uint8)).filter(ImageFilter.GaussianBlur(12)), dtype=np.float64
    )
    out[hole] = blurred[hole]
    return out.astype(np.uint8)


def shape_mask(size: int, oversample: int = 4) -> Image.Image:
    """The Mac's rounded square as an antialiased alpha mask."""
    n = size * oversample
    axis = (np.arange(n) + 0.5) / n * 2 - 1
    x, y = np.meshgrid(axis, axis)
    inside = (np.abs(x) ** EXPONENT + np.abs(y) ** EXPONENT) <= 1
    mask = Image.fromarray((inside * 255).astype(np.uint8), "L")
    return mask.resize((size, size), Image.Resampling.LANCZOS)


def icon(source: Path) -> Image.Image:
    rgb = np.asarray(Image.open(source).convert("RGB"))
    left, top, right, bottom = body_box(rgb)
    square = rgb[top:bottom, left:right]
    square = fill(square, page_mask(square))
    body = Image.fromarray(square).resize((BODY, BODY), Image.Resampling.LANCZOS)
    mask = shape_mask(BODY)
    offset = (CANVAS - BODY) // 2
    canvas = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    # A soft shadow below the square, as the Mac's own icons have.
    shadow = Image.new("L", (CANVAS, CANVAS), 0)
    shadow.paste(mask.point(lambda v: v * 0.45), (offset, offset + 10))
    shadow = shadow.filter(ImageFilter.GaussianBlur(14))
    canvas.paste(Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 255)), (0, 0), shadow)
    canvas.paste(body.convert("RGBA"), (offset, offset), mask)
    return canvas


def main(argv: list[str]) -> int:
    source = Path(argv[0]) if argv else SOURCE
    master = icon(source)
    contents = json.loads((ICONSET / "Contents.json").read_text())
    for entry in contents["images"]:
        points = int(entry["size"].split("x")[0])
        scale = int(entry["scale"].rstrip("x"))
        pixels = points * scale
        name = f"icon_{points}x{points}{'@2x' if scale == 2 else ''}.png"
        master.resize((pixels, pixels), Image.Resampling.LANCZOS).save(ICONSET / name, optimize=True)
        entry["filename"] = name
    (ICONSET / "Contents.json").write_text(json.dumps(contents, indent=2) + "\n")
    print(f"[icon] {len(contents['images'])} sizes from {source.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
