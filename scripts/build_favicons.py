"""
One-shot script: generates all favicon/PWA PNG assets from favicon.svg.

Usage:
    python scripts/build_favicons.py

Requires: pip install cairosvg Pillow
Output files go to static/landing/
"""
import io
import os
import sys

import cairosvg
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_SVG = os.path.join(ROOT, "static", "landing", "favicon.svg")
OUT_DIR = os.path.join(ROOT, "static", "landing")


def svg_to_png(size: int) -> Image.Image:
    png_bytes = cairosvg.svg2png(url=SRC_SVG, output_width=size, output_height=size)
    return Image.open(io.BytesIO(png_bytes)).convert("RGBA")


def save_png(img: Image.Image, name: str):
    path = os.path.join(OUT_DIR, name)
    img.save(path, "PNG")
    print(f"  {name} ({img.size[0]}x{img.size[1]})")


def make_maskable(img512: Image.Image) -> Image.Image:
    # Maskable: safe zone is centre 80% — pad the icon to fill full canvas.
    canvas = Image.new("RGBA", (512, 512), (0, 210, 106, 255))  # #00D26A fill
    inner_size = int(512 * 0.8)
    resized = img512.resize((inner_size, inner_size), Image.LANCZOS)
    offset = (512 - inner_size) // 2
    canvas.paste(resized, (offset, offset), resized)
    return canvas


def main():
    print("Building favicons from", SRC_SVG)

    img16 = svg_to_png(16)
    img32 = svg_to_png(32)
    img48 = svg_to_png(48)
    img180 = svg_to_png(180)
    img192 = svg_to_png(192)
    img512 = svg_to_png(512)

    # favicon.ico — multi-resolution (16, 32, 48)
    ico_path = os.path.join(OUT_DIR, "favicon.ico")
    img48.save(
        ico_path,
        format="ICO",
        sizes=[(16, 16), (32, 32), (48, 48)],
    )
    print(f"  favicon.ico (16/32/48)")

    save_png(img180, "apple-touch-icon.png")
    save_png(img192, "icon-192.png")
    save_png(img512, "icon-512.png")

    maskable = make_maskable(img512)
    save_png(maskable, "icon-512-maskable.png")

    print("Done.")


if __name__ == "__main__":
    main()
