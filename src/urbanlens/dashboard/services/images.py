"""Image processing utilities - EXIF extraction, downscaling, and metadata helpers."""

from __future__ import annotations

import contextlib
from datetime import datetime
from decimal import Decimal
import hashlib
import io
import logging
import math
import posixpath
import re
from typing import IO, TYPE_CHECKING, Any

from django.utils import timezone
from PIL import Image as PILImage
from PIL.ExifTags import GPSTAGS, TAGS

if TYPE_CHECKING:
    from django.core.files.uploadedfile import UploadedFile
    from django.http import HttpRequest

    from urbanlens.dashboard.models.images.model import Image, MediaKind
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


def parse_reposition_payload(body: bytes) -> tuple[Decimal, Decimal]:
    """Parse a photo-reposition JSON payload into validated latitude/longitude Decimals.

    Shared by the pin/wiki/safety gallery reposition endpoints, which all
    accept ``{"latitude": ..., "longitude": ...}`` from a dragged map marker.
    Centralized because each of the three had the same subtle hole: they
    caught ``ValueError`` but ``Decimal("abc")`` raises
    ``decimal.InvalidOperation`` (an ``ArithmeticError``, not a
    ``ValueError``), turning garbage input into a 500 instead of a 400 - and
    ``Decimal("nan")`` parses fine and Postgres ``numeric`` happily stores
    NaN, so nothing rejected non-finite coordinates at all.

    Args:
        body: The raw request body.

    Returns:
        ``(latitude, longitude)`` as finite, in-range Decimals.

    Raises:
        ValueError: On malformed JSON, a non-object payload, missing keys,
            non-numeric/non-finite values, or out-of-range coordinates.
    """
    import json

    try:
        data = json.loads(body)
        latitude = Decimal(str(data["latitude"]))
        longitude = Decimal(str(data["longitude"]))
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise ValueError("Invalid request data.") from exc
    if not (latitude.is_finite() and longitude.is_finite()):
        raise ValueError("Coordinates must be finite numbers.")
    if abs(latitude) > 90 or abs(longitude) > 180:
        raise ValueError("Coordinates out of range.")
    return latitude, longitude


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
    if not (math.isfinite(lat) and math.isfinite(lng)):
        # Some cameras/phones write GPS IFDs with zero-denominator rationals
        # (e.g. "GPS on, no fix yet"), which decode to NaN/Inf - not usable.
        return None
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


# Windows Explorer's XP* EXIF tags, written as null-terminated UTF-16LE byte
# tuples by some cameras/editors. Fallbacks for the standard Artist/
# ImageDescription tags, which not every device populates.
_XP_AUTHOR_TAG = 0x9C9D
_XP_TITLE_TAG = 0x9C9B
_XP_COMMENT_TAG = 0x9C9C

# Common auto-generated phone/camera filename stems: PXL_ (Pixel),
# IMG_/IMG- (Android/iPhone, incl. WhatsApp's IMG-YYYYMMDD-WAxxxx), MVIMG_
# (Google motion photo stills), DSC_/DSCN/DCIM (point-and-shoot cameras). A
# match indicates the uploader almost certainly took the photo themselves,
# as opposed to a descriptively-named file sourced from somewhere else.
_CAMERA_FILENAME_RE = re.compile(r"^(pxl|img|mvimg|dscn|dsc|dcim)[-_]?\d{4,}", re.IGNORECASE)


def _get_ifd0(image_file: IO[bytes]) -> Any | None:
    """Return the top-level (IFD0) EXIF tags for an image file, if present."""
    image_file.seek(0)
    img = PILImage.open(image_file)
    exif = img.getexif()
    return exif or None


def _decode_xp_string(value: Any) -> str | None:
    """Decode a Windows XP* EXIF tag's null-terminated UTF-16LE byte tuple to text."""
    if not value:
        return None
    try:
        raw = bytes(value)
        text = raw.decode("utf-16-le").rstrip("\x00").strip()
    except (TypeError, ValueError, UnicodeDecodeError):
        return None
    return text or None


def extract_author(image_file: IO[bytes]) -> str | None:
    """Return the photo's author/credit from EXIF, or None if absent.

    Prefers the standard ``Artist`` tag; falls back to the Windows-specific
    ``XPAuthor`` tag written by some cameras and editing tools.
    """
    try:
        exif = _get_ifd0(image_file)
    except Exception as exc:
        logger.debug("EXIF author extraction failed: %s", exc)
        return None
    finally:
        with contextlib.suppress(Exception):
            image_file.seek(0)
    if not exif:
        return None
    artist = exif.get(0x013B)  # Artist
    if artist and str(artist).strip():
        return str(artist).strip()
    return _decode_xp_string(exif.get(_XP_AUTHOR_TAG))


def extract_copyright_notice(image_file: IO[bytes]) -> str | None:
    """Return the photo's EXIF copyright notice, or None if absent."""
    try:
        exif = _get_ifd0(image_file)
    except Exception as exc:
        logger.debug("EXIF copyright extraction failed: %s", exc)
        return None
    finally:
        with contextlib.suppress(Exception):
            image_file.seek(0)
    if not exif:
        return None
    notice = exif.get(0x8298)  # Copyright
    if notice and str(notice).strip():
        return str(notice).strip()
    return None


def extract_caption_from_metadata(image_file: IO[bytes]) -> str | None:
    """Return a caption sourced from EXIF ``ImageDescription``/``XPTitle``/``XPComment``."""
    try:
        exif = _get_ifd0(image_file)
    except Exception as exc:
        logger.debug("EXIF caption extraction failed: %s", exc)
        return None
    finally:
        with contextlib.suppress(Exception):
            image_file.seek(0)
    if not exif:
        return None
    description = exif.get(0x010E)  # ImageDescription
    if description and str(description).strip():
        return str(description).strip()
    for tag_id in (_XP_TITLE_TAG, _XP_COMMENT_TAG):
        text = _decode_xp_string(exif.get(tag_id))
        if text:
            return text
    return None


def extract_source_url(image_file: IO[bytes]) -> str | None:
    """Return a source URL embedded in the file's text metadata, if any.

    EXIF has no standard URL tag, but some tools embed one in a PNG text
    chunk, exposed by Pillow via ``Image.info``, under a key like "url" or
    "source".

    Args:
        image_file: The uploaded file or opened FieldFile to read.

    Returns:
        The URL string, or None when no such metadata is present.
    """
    try:
        image_file.seek(0)
        img = PILImage.open(image_file)
        for key, value in (img.info or {}).items():
            if isinstance(key, str) and isinstance(value, str) and key.lower() in {"url", "source", "source_url"} and value.strip().lower().startswith(("http://", "https://")):
                return value.strip()
    except Exception as exc:
        logger.debug("Source URL extraction failed: %s", exc)
        return None
    finally:
        with contextlib.suppress(Exception):
            image_file.seek(0)
    return None


def is_camera_generated_filename(filename: str) -> bool:
    """Return True when a filename matches common phone/camera auto-naming conventions.

    Used to infer that the uploader is the photo's author when no attribution
    metadata (author/source URL/caption/copyright) is present at all - a
    generically-named camera file (e.g. ``PXL_20260709_123456.jpg``) is very
    unlikely to be a photo sourced from somewhere else.

    Args:
        filename: The stored or uploaded filename (path or bare name).

    Returns:
        True when the filename's stem matches a known camera naming pattern.
    """
    stem = posixpath.splitext(posixpath.basename(filename))[0]
    return bool(_CAMERA_FILENAME_RE.match(stem))


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


def downscale_stored_image(image: Image, max_dimension: int | None, convert_webp: bool, strip_gps: bool = False) -> int | None:
    """Downscale and/or re-encode an Image's stored file in place.

    The stored file is replaced only when processing actually shrinks it (or a
    WebP conversion was requested), unless ``strip_gps`` removed an embedded
    GPS tag - that always forces a re-save regardless of the resulting size,
    since leaving the original file in place would defeat the point. The
    caller is responsible for persisting ``image.image.name`` and the returned
    size to the database - this function only touches storage.

    Args:
        image: The Image row whose stored file to process.
        max_dimension: Longest-edge cap in pixels, or None to keep dimensions.
        convert_webp: Whether to re-encode the file as WebP.
        strip_gps: When True, removes any embedded GPS EXIF tag from the
            stored file's own metadata (independent of the derived
            ``Image.latitude``/``longitude`` fields, which the caller controls
            separately).

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
        exif_bytes = img.info.get("exif")
        has_gps = False
        if strip_gps and exif_bytes:
            exif = img.getexif()
            if exif.get_ifd(0x8825):  # 34853 - GPSInfo IFD
                del exif[0x8825]
                exif_bytes = exif.tobytes()
                has_gps = True
        if not needs_resize and not needs_convert and not has_gps:
            return None
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

    # A pure resize that somehow grew the file is not worth keeping - unless
    # stripping GPS was the whole reason we're here, in which case keeping the
    # smaller-but-still-tagged original would defeat the point.
    if not needs_convert and not has_gps and new_size >= old_size:
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


def image_upload_error(file_obj: UploadedFile, declared_media_type: MediaKind) -> tuple[str, int] | None:
    """Run every pre-storage safety check an uploaded file must pass, in order.

    Every endpoint that creates an ``Image`` row from a user-uploaded file
    should call this immediately before ``Image.objects.create(...)`` -
    checks, in order: the site-wide max file size, magic-byte content-type
    sniffing (catching a mislabeled/spoofed upload before it's trusted as
    whatever ``declared_media_type`` claims), and antivirus scanning. Quota
    is deliberately NOT checked here - it's scope-dependent (per-pin,
    per-wiki, per-profile) and each call site already checks it separately
    against the right queryset.

    Args:
        file_obj: The uploaded file.
        declared_media_type: The ``MediaKind`` the caller expects/classified
            this upload as.

    Returns:
        ``(message, status_code)`` for the first failing check, or ``None``
        if the file passes every check and is safe to store.
    """
    from urbanlens.dashboard.services.content_sniffing import content_type_mismatch_error
    from urbanlens.dashboard.services.malware_scan import MalwareScanUnavailableError, malware_error_for_upload
    from urbanlens.dashboard.services.storage import file_size_error_for_upload

    size_error = file_size_error_for_upload(file_obj.size)
    if size_error:
        return size_error, 413

    sniff_error = content_type_mismatch_error(file_obj, declared_media_type)
    if sniff_error:
        return sniff_error, 400

    try:
        malware_error = malware_error_for_upload(file_obj)
    except MalwareScanUnavailableError:
        return "Our antivirus scanner is temporarily unavailable. Please try again shortly.", 503
    if malware_error:
        return malware_error, 422

    return None


def image_to_gallery_json(img: Image, request: HttpRequest, viewer_profile: Profile | None = None) -> dict:
    """Serialize an Image to a dict suitable for a photo gallery/map layer.

    Shared by the pin, location wiki, and safety check-in gallery views so
    the upload response and map layer JSON stay in the same shape everywhere.

    Args:
        img: The image to serialize.
        request: Current request, used to build an absolute image URL.
        viewer_profile: The requesting profile, if any - used to flag ``is_mine``.

    Returns:
        Dict with id/url/caption/latitude/longitude/uploader/is_mine, plus the
        attribution fields (author/source_url/copyright/taken_at) shown in the
        lightbox.
    """
    return {
        "id": img.pk,
        "url": request.build_absolute_uri(img.image.url),
        "caption": img.caption or "",
        "latitude": float(img.latitude) if img.latitude is not None else None,
        "longitude": float(img.longitude) if img.longitude is not None else None,
        "uploader": img.profile.username if img.profile else "",
        "is_mine": viewer_profile is not None and img.profile_id == viewer_profile.pk,
        "author": img.author or "",
        "source_url": img.source_url or "",
        "copyright": img.copyright or "",
        "taken_at": img.taken_at.isoformat() if img.taken_at else None,
    }
