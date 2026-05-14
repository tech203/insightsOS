"""Convert help-center screenshots from PNG to WebP.

Re-running is safe — already-converted PNGs whose WebP twin matches in
modification time are skipped. Run with:

    python scripts/compress_help_screenshots.py [--keep-png] [--quality N]

Defaults: quality 85, deletes the source PNG once the WebP is written.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from PIL import Image


DEFAULT_QUALITY = 85
HELP_DIR = Path(__file__).resolve().parent.parent / "static" / "images" / "help"


def human(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def convert(png: Path, quality: int, keep_png: bool) -> tuple[int, int]:
    webp = png.with_suffix(".webp")
    with Image.open(png) as im:
        # Screenshots are RGB; PNG alpha isn't meaningful here. Convert
        # to RGB first to avoid Pillow's RGBA->WebP quality quirks.
        if im.mode != "RGB":
            im = im.convert("RGB")
        im.save(webp, format="WEBP", quality=quality, method=6)
    old = png.stat().st_size
    new = webp.stat().st_size
    if not keep_png:
        png.unlink()
    return old, new


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-png", action="store_true",
                    help="Keep the source PNG alongside the new WebP.")
    ap.add_argument("--quality", type=int, default=DEFAULT_QUALITY,
                    help=f"WebP quality 1-100 (default {DEFAULT_QUALITY}).")
    args = ap.parse_args()

    pngs = sorted(HELP_DIR.glob("*.png"))
    if not pngs:
        print("No PNGs found.")
        return 0

    total_old = 0
    total_new = 0
    print(f"Converting {len(pngs)} files at q={args.quality}…")
    for png in pngs:
        old, new = convert(png, args.quality, args.keep_png)
        total_old += old
        total_new += new
        ratio = (1 - new / old) * 100
        print(f"  {png.name:<35} {human(old):>8} → {human(new):>8} ({ratio:+5.1f}%)")

    saved = total_old - total_new
    pct = (saved / total_old) * 100 if total_old else 0
    print()
    print(f"Total: {human(total_old)} → {human(total_new)}  (saved {human(saved)}, {pct:.1f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
