#!/usr/bin/env python3
"""Batch-convert images in a directory into 125x125 WebP thumbnails with EXIF stripped.

For each input file "foo_bar.jpg" (or .jpeg/.png/.webp), writes
"foo_bar.webp" to a "converted/" subdirectory (or custom -o/--outdir).

Usage:
    python convert_to_webp.py path/to/image_dir
    python convert_to_webp.py path/to/image_dir -o /path/to/output/dir
    python convert_to_webp.py path/to/image_dir -q 80
"""

import argparse
from pathlib import Path
import sys

from PIL import Image

SIZE = (125, 125)
INPUT_EXTS = (".png", ".jpg", ".jpeg", ".webp")


def unique_path(path: Path) -> Path:
    """Avoid collisions (e.g. foo.jpg and foo.png both -> foo.webp)."""
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    n = 2
    while True:
        candidate = parent / f"{stem}_{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def convert_image(src_path: Path, dest_path: Path, quality: int) -> None:
    with Image.open(src_path) as opened:
        # Preserve transparency; don't rebind the `with` target so the file still closes.
        if opened.mode not in ("RGB", "RGBA"):
            img = opened.convert("RGBA" if "A" in opened.getbands() else "RGB")
        else:
            img = opened

        img = img.resize(SIZE, Image.LANCZOS)

        # Fresh image with pixels only - strips EXIF/ICC/XMP.
        clean = Image.new(img.mode, img.size)
        clean.paste(img)

        clean.save(
            dest_path,
            format="WEBP",
            quality=quality,
            method=6,  # slowest/best compression effort
            exif=b"",  # explicitly strip EXIF
        )


def main():
    parser = argparse.ArgumentParser(description="Convert every PNG/JPG/WebP in a directory to a 125x125 optimized WebP with EXIF stripped.")
    parser.add_argument("indir", type=Path, help="Directory containing source images")
    parser.add_argument("-o", "--outdir", type=Path, default=None, help="Output directory (default: <indir>/converted)")
    parser.add_argument("-q", "--quality", type=int, default=80, help="WebP quality, 1-100 (default: 80). Lower = smaller file size.")
    args = parser.parse_args()

    indir = args.indir
    if not indir.is_dir():
        print(f"Error: not a directory: {indir}", file=sys.stderr)
        sys.exit(1)

    outdir = args.outdir if args.outdir is not None else (indir / "converted")
    outdir.mkdir(parents=True, exist_ok=True)

    sources = sorted(p for p in indir.iterdir() if p.is_file() and p.suffix.lower() in INPUT_EXTS)

    if not sources:
        print(f"No PNG/JPG/WebP files found in {indir}")
        return

    converted = 0
    failed = 0
    for src in sources:
        dest = unique_path(outdir / f"{src.stem}.webp")
        try:
            convert_image(src, dest, args.quality)
        except Exception as e:
            print(f"Failed: {src.name} ({e})", file=sys.stderr)
            failed += 1
            continue
        size_kb = dest.stat().st_size / 1024
        print(f"Saved: {dest} ({size_kb:.1f} KB)")
        converted += 1

    print(f"\nDone: {converted} converted, {failed} failed.")


if __name__ == "__main__":
    main()
