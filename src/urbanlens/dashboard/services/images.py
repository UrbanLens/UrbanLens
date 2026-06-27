"""Image processing utilities — EXIF extraction and metadata helpers."""

from __future__ import annotations

import contextlib
import logging
from typing import IO, Any

from PIL import Image as PILImage
from PIL.ExifTags import GPSTAGS

logger = logging.getLogger(__name__)


def _get_gps_ifd(image_file: IO[bytes]) -> dict[int, Any] | None:
    """Return the raw EXIF GPS IFD for an image file, if present."""
    image_file.seek(0)
    img = PILImage.open(image_file)
    exif = img.getexif()
    if not exif:
        return None
    return exif.get_ifd(0x8825) or None  # 34853 - GPSInfo IFD tag


def _dms_to_decimal(dms: tuple[float, ...], ref: str) -> float:
    """Convert a DMS tuple from EXIF to a signed decimal degree."""
    degrees, minutes, seconds = (float(x) for x in dms)
    decimal = degrees + minutes / 60.0 + seconds / 3600.0
    if ref in {"S", "W"}:
        decimal = -decimal
    return decimal


def extract_gps_coords(image_file: IO[bytes]) -> tuple[float, float] | None:
    """Return (latitude, longitude) from EXIF GPS tags, or None if not present."""
    try:
        gps_ifd = _get_gps_ifd(image_file)
    except Exception as exc:
        logger.debug("EXIF GPS extraction failed: %s", exc)
        return None
    finally:
        with contextlib.suppress(Exception):
            image_file.seek(0)

    if not gps_ifd:
        return None
    gps_data = {GPSTAGS.get(k, k): v for k, v in gps_ifd.items()}
    if "GPSLatitude" not in gps_data or "GPSLongitude" not in gps_data:
        return None
    lat = _dms_to_decimal(gps_data["GPSLatitude"], gps_data.get("GPSLatitudeRef", "N"))
    lng = _dms_to_decimal(gps_data["GPSLongitude"], gps_data.get("GPSLongitudeRef", "E"))
    return lat, lng
