"""Image processing utilities - EXIF extraction, downscaling, and metadata helpers."""

from __future__ import annotations

import contextlib
from datetime import datetime
import hashlib
import io
import logging
import math
import posixpath
from typing import IO, TYPE_CHECKING, Any

from django.utils import timezone
from PIL import Image as PILImage
from PIL.ExifTags import GPSTAGS, TAGS

if TYPE_CHECKING:
    from django.http import HttpRequest

    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

_EXIF_DATETIME_FORMAT = "%Y:%m:%d %H:%M:%S"

# Formats the downscale pipeline will re-encode. Anything else (animated GIFs,
# exotic formats) is stored untouched - only its size is counted.
_PROCESSABLE_FORMATS = {"JPEG", "PNG", "WEBP", "TIFF"}

_FORMAT_EXTENSIONS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "TIFF": ".tif"}

# Cap for binary EXIF payloads (e.g. MakerNote blobs) stored as hex in the
# JSON snapshot; larger values are summarized instead of embedded.
_EXIF_BYTES_HEX_LIMIT = 4096


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


def _json_safe(value: Any) -> Any:
    """Convert an EXIF value into something JSON-serializable.

    PIL yields rationals (IFDRational), bytes, tuples, and nested dicts;
    everything is reduced to numbers, strings, lists, and dicts. Binary blobs
    are hex-encoded up to a size cap, then summarized.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        # NaN/inf are not valid JSON; stringify them.
        return value if math.isfinite(value) else str(value)
    if isinstance(value, bytes):
        if len(value) * 2 > _EXIF_BYTES_HEX_LIMIT:
            return f"<{len(value)} bytes>"
        return value.hex()
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    # IFDRational and friends: prefer a number, fall back to a string.
    try:
        return _json_safe(float(value))
    except (TypeError, ValueError, ZeroDivisionError):
        return str(value)


def extract_exif_data(image_file: IO[bytes]) -> dict[str, Any] | None:
    """Snapshot all EXIF metadata from an image file as a JSON-safe dict.

    Captured before any downscaling/re-encoding so nothing is lost if the
    stored file is converted. Top-level (IFD0) and Exif SubIFD tags are merged
    by tag name; GPS tags are nested under ``GPSInfo``.

    Args:
        image_file: The uploaded file or opened FieldFile to read.

    Returns:
        The EXIF data keyed by human-readable tag names, or None when the
        image has no EXIF data or cannot be parsed.
    """
    try:
        image_file.seek(0)
        img = PILImage.open(image_file)
        exif = img.getexif()
        if not exif:
            return None
        data: dict[str, Any] = {}
        for tag_id, value in exif.items():
            data[str(TAGS.get(tag_id, tag_id))] = _json_safe(value)
        exif_ifd = exif.get_ifd(0x8769)  # 34665 - Exif SubIFD
        for tag_id, value in exif_ifd.items():
            data[str(TAGS.get(tag_id, tag_id))] = _json_safe(value)
        gps_ifd = exif.get_ifd(0x8825)  # 34853 - GPSInfo IFD
        if gps_ifd:
            data["GPSInfo"] = {str(GPSTAGS.get(tag_id, tag_id)): _json_safe(value) for tag_id, value in gps_ifd.items()}
        return data or None
    except Exception as exc:
        logger.debug("EXIF snapshot failed: %s", exc)
        return None
    finally:
        with contextlib.suppress(Exception):
            image_file.seek(0)


def downscale_stored_image(image: Image, max_dimension: int | None, convert_webp: bool) -> int | None:
    """Downscale and/or re-encode an Image's stored file in place.

    The stored file is replaced only when processing actually shrinks it (or a
    WebP conversion was requested); otherwise it is left untouched. The
    caller is responsible for persisting ``image.image.name`` and the returned
    size to the database - this function only touches storage.

    Args:
        image: The Image row whose stored file to process.
        max_dimension: Longest-edge cap in pixels, or None to keep dimensions.
        convert_webp: Whether to re-encode the file as WebP.

    Returns:
        The new stored size in bytes when the file was replaced, else None.

    Raises:
        OSError: When the file cannot be read from or written to storage.
    """
    old_name = image.image.name
    if not old_name:
        return None
    old_size = image.image.size
    with image.image.open("rb") as stored_file:
        img: PILImage.Image = PILImage.open(stored_file)
        source_format = (img.format or "").upper()
        if source_format not in _PROCESSABLE_FORMATS:
            return None
        needs_resize = max_dimension is not None and max(img.size) > max_dimension
        needs_convert = convert_webp and source_format != "WEBP"
        if not needs_resize and not needs_convert:
            return None
        exif_bytes = img.info.get("exif")
        icc_profile = img.info.get("icc_profile")
        img.load()

    if needs_resize and max_dimension is not None:
        img.thumbnail((max_dimension, max_dimension), PILImage.Resampling.LANCZOS)

    target_format = "WEBP" if convert_webp else source_format
    save_kwargs: dict[str, Any] = {}
    if target_format == "WEBP":
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA" if "A" in img.mode or img.mode == "P" else "RGB")
        save_kwargs.update(quality=85, method=4)
    elif target_format == "JPEG":
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        save_kwargs.update(quality=85, optimize=True)
    elif target_format == "PNG":
        save_kwargs.update(optimize=True)
    if exif_bytes and target_format in {"JPEG", "WEBP", "TIFF"}:
        save_kwargs["exif"] = exif_bytes
    if icc_profile:
        save_kwargs["icc_profile"] = icc_profile

    buffer = io.BytesIO()
    img.save(buffer, format=target_format, **save_kwargs)
    new_size = buffer.tell()

    # A pure resize that somehow grew the file is not worth keeping.
    if not needs_convert and new_size >= old_size:
        return None

    from django.core.files.base import ContentFile

    stem = posixpath.splitext(posixpath.basename(old_name))[0]
    image.image.save(f"{stem}{_FORMAT_EXTENSIONS[target_format]}", ContentFile(buffer.getvalue()), save=False)
    if image.image.name != old_name:
        with contextlib.suppress(OSError):
            image.image.storage.delete(old_name)
    logger.info("Downscaled image %s: %s -> %s bytes (%s)", image.pk, old_size, new_size, target_format)
    return new_size


def compute_checksum(image_file: IO[bytes]) -> str:
    """Compute the SHA-256 hex digest of an uploaded image file.

    Used to detect duplicate uploads: two files with the same digest are the
    same photo. The file position is rewound before and after hashing so the
    file can still be saved afterwards.

    Args:
        image_file: The file to hash (an UploadedFile or an opened FieldFile).

    Returns:
        The 64-character lowercase hex digest.
    """
    image_file.seek(0)
    digest = hashlib.sha256()
    for chunk in iter(lambda: image_file.read(1024 * 1024), b""):
        digest.update(chunk)
    image_file.seek(0)
    return digest.hexdigest()


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
