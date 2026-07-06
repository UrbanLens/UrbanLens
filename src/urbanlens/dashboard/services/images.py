"""Image processing utilities - EXIF extraction and metadata helpers."""

from __future__ import annotations

import contextlib
from datetime import datetime
import logging
from typing import IO, TYPE_CHECKING, Any

from django.utils import timezone
from PIL import Image as PILImage
from PIL.ExifTags import GPSTAGS

if TYPE_CHECKING:
    from django.http import HttpRequest

    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

_EXIF_DATETIME_FORMAT = "%Y:%m:%d %H:%M:%S"


def _get_gps_ifd(image_file: IO[bytes]) -> dict[int, Any] | None:
    """Return the raw EXIF GPS IFD for an image file, if present."""
    image_file.seek(0)
    img = PILImage.open(image_file)
    exif = img.getexif()
    if not exif:
        return None
    return exif.get_ifd(0x8825) or None  # 34853 - GPSInfo IFD tag


def _get_exif_ifd(image_file: IO[bytes]) -> dict[int, Any] | None:
    """Return the raw EXIF "Exif" SubIFD for an image file, if present."""
    image_file.seek(0)
    img = PILImage.open(image_file)
    exif = img.getexif()
    if not exif:
        return None
    return exif.get_ifd(0x8769) or None  # 34665 - Exif SubIFD tag


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


def extract_taken_at(image_file: IO[bytes]) -> datetime | None:
    """Return the EXIF DateTimeOriginal capture time, or None if absent/unparseable.

    EXIF datetimes carry no timezone offset, so the result is made timezone-aware
    using the server's local time rather than the photo's actual capture location.
    """
    try:
        exif_ifd = _get_exif_ifd(image_file)
    except Exception as exc:
        logger.debug("EXIF DateTimeOriginal extraction failed: %s", exc)
        return None
    finally:
        with contextlib.suppress(Exception):
            image_file.seek(0)

    if not exif_ifd:
        return None
    raw_value = exif_ifd.get(0x9003)  # 36867 - DateTimeOriginal
    if not raw_value:
        return None
    try:
        naive = datetime.strptime(str(raw_value), _EXIF_DATETIME_FORMAT)
    except ValueError:
        logger.debug("Unparseable EXIF DateTimeOriginal value: %s", raw_value)
        return None
    return timezone.make_aware(naive) if timezone.is_naive(naive) else naive


def image_to_gallery_json(img: Image, request: HttpRequest, viewer_profile: Profile | None = None) -> dict:
    """Serialize an Image to a dict suitable for a photo gallery/map layer.

    Shared by the pin, location wiki, and safety check-in gallery views so
    the upload response and map layer JSON stay in the same shape everywhere.

    Args:
        img: The image to serialize.
        request: Current request, used to build an absolute image URL.
        viewer_profile: The requesting profile, if any - used to flag ``is_mine``.

    Returns:
        Dict with id/url/caption/latitude/longitude/uploader/is_mine.
    """
    return {
        "id": img.pk,
        "url": request.build_absolute_uri(img.image.url),
        "caption": img.caption or "",
        "latitude": float(img.latitude) if img.latitude is not None else None,
        "longitude": float(img.longitude) if img.longitude is not None else None,
        "uploader": img.profile.username if img.profile else "",
        "is_mine": viewer_profile is not None and img.profile_id == viewer_profile.pk,
    }
