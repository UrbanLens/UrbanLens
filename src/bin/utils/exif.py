#!/usr/bin/env python3
"""
Print all discoverable metadata for an image file.

This isn't explicitly needed for anything, but is helpful for debugging and creating new code for different kinds of photos.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
from typing import Any

from PIL import ExifTags, Image


def print_exiftool(path: Path) -> bool:
    """Print metadata using ExifTool, if installed."""
    if not shutil.which("exiftool"):
        return False

    result = subprocess.run(
        [
            "exiftool",
            "-a",  # show duplicate tags
            "-u",  # show unknown tags
            "-g1",  # group by metadata family
            "-s",  # short tag names
            "-json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    data = json.loads(result.stdout)
    print("=== ExifTool metadata ===")
    print(json.dumps(data[0], indent=2, ensure_ascii=False, default=str))
    return True


def decode_exif_value(value: Any) -> Any:
    """Make EXIF values printable."""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.hex()
    if isinstance(value, tuple):
        return [decode_exif_value(v) for v in value]
    return value


def print_pillow_metadata(path: Path) -> None:
    """Print metadata visible through Pillow."""
    print("\n=== Pillow metadata ===")

    with Image.open(path) as img:
        print(f"Format: {img.format}")
        print(f"Mode: {img.mode}")
        print(f"Size: {img.size}")
        print(f"Info keys: {sorted(img.info.keys())}")

        if img.info:
            print("\n--- img.info ---")
            for key, value in img.info.items():
                if isinstance(value, bytes):
                    printable = f"<{len(value)} bytes>"
                    if isinstance(key, str) and key.lower() in {"exif", "xmp", "xml", "comment"}:
                        try:
                            printable = value.decode("utf-8", errors="replace")
                        except Exception:
                            printable = value.hex()
                else:
                    printable = value
                print(f"{key}: {printable}")

        exif = img.getexif()
        if not exif:
            print("\nNo Pillow EXIF found.")
            return

        print("\n--- EXIF tags ---")
        for tag_id, value in exif.items():
            tag_name = ExifTags.TAGS.get(tag_id, tag_id)
            print(f"{tag_name}: {decode_exif_value(value)}")

        gps_ifd_id = 34853
        gps_data = exif.get_ifd(gps_ifd_id) if gps_ifd_id in exif else None

        if gps_data:
            print("\n--- GPS tags ---")
            for gps_id, value in gps_data.items():
                gps_name = ExifTags.GPSTAGS.get(gps_id, gps_id)
                print(f"{gps_name}: {decode_exif_value(value)}")
        else:
            print("\nNo Pillow GPS EXIF found.")


def print_raw_metadata_markers(path: Path) -> None:
    """Look for embedded metadata blocks by raw byte markers."""
    print("\n=== Raw marker scan ===")

    data = path.read_bytes()

    markers = {
        b"Exif\x00\x00": "EXIF block",
        b"http://ns.adobe.com/xap/1.0/": "XMP block",
        b"Photoshop 3.0": "Photoshop/IPTC block",
        b"ICC_PROFILE": "ICC profile",
        b"JFIF": "JFIF marker",
    }

    found = False
    for marker, label in markers.items():
        index = data.find(marker)
        if index >= 0:
            found = True
            print(f"{label}: found at byte offset {index}")

    if not found:
        print("No common raw metadata markers found.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("photo", type=Path)
    args = parser.parse_args()

    path = args.photo.expanduser().resolve()

    if not path.exists():
        raise FileNotFoundError(path)

    print(f"File: {path}")
    print(f"Size: {path.stat().st_size:,} bytes")

    used_exiftool = print_exiftool(path)

    if not used_exiftool:
        print("ExifTool not found. Falling back to Pillow-only inspection.")
        print("For best results, install ExifTool.")

    print_pillow_metadata(path)
    print_raw_metadata_markers(path)


if __name__ == "__main__":
    main()
