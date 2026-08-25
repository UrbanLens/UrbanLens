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

if TYPE_CHECKING:
    from collections.abc import Collection

from django.utils import timezone
from PIL import Image as PILImage, ImageOps
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

_FORMAT_EXTENSIONS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "TIFF": ".tif", "AVIF": ".avif", "HEIF": ".heic"}

# Formats whose stored file we can rewrite carrying modified EXIF. A superset of
# _PROCESSABLE_FORMATS on purpose: those are the formats the *downscaler* will
# re-encode, whereas these are the ones a GPS strip can be honoured for. Keeping
# the two separate is what stops "we would never resize an AVIF" from silently
# turning into "we never scrub an AVIF's coordinates either".
# HEIF covers both .heic and .heif - pillow-heif registers one opener reporting
# format "HEIF" for both, so the extension a phone happens to use does not
# change what this pipeline sees.
_EXIF_REWRITABLE_FORMATS = _PROCESSABLE_FORMATS | {"AVIF", "HEIF"}

# Formats that must be re-encoded whatever the downscale policy says, because no
# mainstream browser outside Safari renders them and stored photos are served
# through a plain <img src>. Accepting the upload and keeping the bytes verbatim
# swaps an explicit "convert it to JPEG first" refusal for a broken image, which
# is strictly worse. `_MUST_TRANSCODE_TARGET` is the format they land in when the
# uploader's policy does not already pick one (a subscriber with downscaling off
# and no WebP conversion, which is the default).
#
# AVIF is deliberately not here: browser support for it is broad, so re-encoding
# one would cost quality for nothing.
_MUST_TRANSCODE_FORMATS = {"HEIF"}
_MUST_TRANSCODE_TARGET = "JPEG"

# EXIF tag 34853 - the GPSInfo IFD pointer.
_GPS_IFD_TAG = 0x8825

# Cap for binary EXIF payloads (e.g. MakerNote blobs) stored as hex in the
# JSON snapshot; larger values are summarized instead of embedded.
_EXIF_BYTES_HEX_LIMIT = 4096


def coerce_coordinates(data: Any) -> tuple[Decimal, Decimal]:
    """Validate and convert a mapping's ``latitude``/``longitude`` entries into Decimals.

    Shared by ``parse_reposition_payload`` (raw JSON body) and any caller
    that already has a parsed request-data mapping (e.g. DRF's
    ``request.data``). Centralized because ``Decimal("abc")`` raises
    ``decimal.InvalidOperation`` (an ``ArithmeticError``, not a
    ``ValueError``), and ``Decimal("nan")`` parses fine and Postgres
    ``numeric`` happily stores NaN, so neither is safe to skip validating.

    Args:
        data: The parsed request payload, expected to be a mapping with
            ``latitude``/``longitude`` keys.

    Returns:
        ``(latitude, longitude)`` as finite, in-range Decimals.

    Raises:
        ValueError: On a non-mapping payload, missing keys,
            non-numeric/non-finite values, or out-of-range coordinates.
    """
    try:
        latitude = Decimal(str(data["latitude"]))
        longitude = Decimal(str(data["longitude"]))
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise ValueError("Invalid request data.") from exc
    if not (latitude.is_finite() and longitude.is_finite()):
        raise ValueError("Coordinates must be finite numbers.")
    if abs(latitude) > 90 or abs(longitude) > 180:
        raise ValueError("Coordinates out of range.")
    return latitude, longitude


def parse_reposition_payload(body: bytes) -> tuple[Decimal, Decimal]:
    """Parse a photo-reposition JSON payload into validated latitude/longitude Decimals.

    Shared by the pin/wiki/safety gallery reposition endpoints, which all
    accept ``{"latitude": ..., "longitude": ...}`` from a dragged map marker.

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
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid request data.") from exc
    return coerce_coordinates(data)


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


def extract_gps_direction(image_file: IO[bytes]) -> float | None:
    """Return the compass bearing the camera was pointing, or None if absent.

    Prefers ``GPSImgDirection`` (the direction the camera itself was facing -
    what a "same place, same angle over time" comparison actually needs);
    falls back to ``GPSDestBearing`` (direction *to* a destination point) only
    when a device wrote that instead, which happens on some cameras. Neither
    tag's *Ref* companion (``"T"`` true north vs ``"M"`` magnetic north) is
    preserved as a separate field - like ``GPSLatitudeRef``/``GPSLongitudeRef``
    above, it's a single already-decided reference frame per photo, not a
    per-record ambiguity worth threading through the rest of the app for.

    Args:
        image_file: The uploaded/stored image file to read EXIF from.

    Returns:
        A bearing in degrees, normalized to ``[0, 360)``, or ``None`` if the
        image has no GPS IFD or neither direction tag.
    """
    try:
        gps_ifd = _get_gps_ifd(image_file)
    except Exception as exc:
        logger.debug("EXIF GPS direction extraction failed: %s", exc)
        return None
    finally:
        with contextlib.suppress(Exception):
            image_file.seek(0)

    if not gps_ifd:
        return None
    gps_data = {GPSTAGS.get(k, k): v for k, v in gps_ifd.items()}
    raw = gps_data.get("GPSImgDirection", gps_data.get("GPSDestBearing"))
    if raw is None:
        return None
    try:
        direction = float(raw) % 360.0
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    if not math.isfinite(direction):
        # Same zero-denominator-rational failure mode as extract_gps_coords.
        return None
    return direction


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


#: Extensions that reach `_MUST_TRANSCODE_FORMATS`. Used only to decide whether
#: the downscale pass is worth entering at all - the authoritative check is
#: Pillow's reported format once the file is open, so a mislabelled file costs
#: one wasted open and nothing else.
_MUST_TRANSCODE_EXTENSIONS = {".heic", ".heif"}


def stored_file_needs_transcode(name: str) -> bool:
    """Whether a stored upload must be re-encoded regardless of downscale policy.

    A subscriber with downscaling off, WebP conversion off and location
    stripping off never entered the downscale pass, so their HEIC was served
    verbatim to browsers that cannot render it.

    Args:
        name: The stored file's name.

    Returns:
        True when the file's extension is one that must be transcoded.
    """
    return posixpath.splitext(name or "")[1].lower() in _MUST_TRANSCODE_EXTENSIONS


def downscale_stored_image(image: Image, max_dimension: int | None, convert_webp: bool) -> int | None:
    """Downscale, re-encode, and strip EXIF from an Image's stored file in place.

    The stored file is replaced when processing shrinks it, when a WebP
    conversion was requested, **or** when it carries an EXIF block - that last
    one regardless of the resulting size, since leaving the original in place is
    exactly the leak. The caller persists ``image.image.name`` and the returned
    size; this function only touches storage.

    EXIF removal is unconditional and not a setting. The block identifies the
    camera and often the place, and a stored file is served to everybody who can
    reach the container it was contributed to. The values are kept on the
    ``Image`` row (``exif_data``, ``latitude``/``longitude``, ``taken_at``),
    where the app's own visibility rules apply to them.

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
        processable = source_format in _PROCESSABLE_FORMATS
        if not processable and source_format not in _EXIF_REWRITABLE_FORMATS:
            return None
        # Resizing/converting stays limited to _PROCESSABLE_FORMATS. A GPS strip
        # does not: it has to happen for any format we can rewrite at all, since
        # the alternative is leaving coordinates in a file the user asked us to
        # scrub. AVIF is the case that matters - phones produce it, it carries a
        # GPS IFD, and it is not a format this pipeline would otherwise touch.
        must_transcode = source_format in _MUST_TRANSCODE_FORMATS
        # A format being re-encoded anyway can be resized in the same pass, so
        # the resize gate follows "will this file be rewritten", not the narrower
        # "would the downscaler normally touch this format".
        rewriting = processable or must_transcode
        needs_resize = rewriting and max_dimension is not None and max(img.size) > max_dimension
        needs_convert = (processable and convert_webp and source_format != "WEBP") or must_transcode
        # Checked off getexif() as well as info["exif"], because TIFF carries EXIF
        # in its own native IFD and leaves info["exif"] unset - gating on that key
        # alone meant a tagged TIFF was never even examined.
        has_exif = bool(img.info.get("exif")) or bool(img.getexif())
        # A file needing neither a resize nor a conversion is still rewritten when
        # it carries EXIF, since leaving the original in place is the whole leak.
        if not needs_resize and not needs_convert and not has_exif:
            return None
        icc_profile = img.info.get("icc_profile")
        img.load()
        # Orientation is the one tag that changes what the file looks like, so it
        # is applied to the pixels here - before the block is dropped on save,
        # and while the tag is still there to read. A no-op when absent.
        img = ImageOps.exif_transpose(img) or img

    if needs_resize and max_dimension is not None:
        img.thumbnail((max_dimension, max_dimension), PILImage.Resampling.LANCZOS)

    target_format = "WEBP" if convert_webp else (_MUST_TRANSCODE_TARGET if must_transcode else source_format)
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
    # No `exif=` is ever passed: the block is recorded on the Image row and must
    # not travel with a file we serve to a whole wiki. Orientation was the reason
    # it used to be re-attached, and exif_transpose above has already spent it on
    # the pixels. Note this is an omission that has to stay an omission - Pillow
    # writes nothing unless asked, but an encoder that carries EXIF through on its
    # own (pillow-heif does) would need the block cleared rather than merely not
    # supplied, which is why HEIF is transcoded rather than rewritten in place.
    if icc_profile:
        save_kwargs["icc_profile"] = icc_profile

    buffer = io.BytesIO()
    img.save(buffer, format=target_format, **save_kwargs)
    new_size = buffer.tell()

    # A pure resize that somehow grew the file is not worth keeping - unless the
    # EXIF strip was the whole reason we're here, in which case keeping the
    # smaller-but-still-tagged original would defeat the point.
    if not needs_convert and not has_exif and new_size >= old_size:
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


def image_upload_error(file_obj: UploadedFile, declared_media_type: MediaKind, *, skip_malware_scan: bool = False) -> tuple[str, int] | None:
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
        skip_malware_scan: When True, skips the antivirus scan - only the
            fast, local size/content-type checks run. For callers that scan
            asynchronously after accepting the upload (see
            ``controllers.comments``/``tasks.scan_comment_image``) instead of
            blocking the request on a clamd round-trip.

    Returns:
        ``(message, status_code)`` for the first failing check, or ``None``
        if the file passes every check and is safe to store.
    """
    from urbanlens.dashboard.models.images.model import MediaKind
    from urbanlens.dashboard.services.media.storage import file_size_error_for_upload
    from urbanlens.dashboard.services.security.content_sniffing import content_type_mismatch_error, photo_is_not_an_image_error, unsupported_image_extension_error

    size_error = file_size_error_for_upload(file_obj.size)
    if size_error:
        return size_error, 413

    # Before sniffing, because sniffing fails open on formats with no magic-byte
    # signature - which is exactly what SVG is. See the helper for why the stored
    # extension, not the bytes, is the thing that has to be constrained here.
    if declared_media_type == MediaKind.PHOTO:
        extension_error = unsupported_image_extension_error(file_obj.name or "")
        if extension_error:
            return extension_error, 400

        # And the bytes have to actually be an image. The general sniff below
        # fails open on anything it cannot fingerprint, which is right for
        # documents and wrong here - a shell script named .png is unrecognisable
        # rather than mismatched, so it sailed through and was stored, then
        # served back from this origin as an image.
        not_an_image = photo_is_not_an_image_error(file_obj)
        if not_an_image:
            return not_an_image, 400

    sniff_error = content_type_mismatch_error(file_obj, declared_media_type)
    if sniff_error:
        return sniff_error, 400

    if skip_malware_scan:
        return None

    from urbanlens.dashboard.services.security.malware_scan import MalwareScanUnavailableError, malware_error_for_upload

    try:
        malware_error = malware_error_for_upload(file_obj)
    except MalwareScanUnavailableError:
        return "Our antivirus scanner is temporarily unavailable. Please try again shortly.", 503
    if malware_error:
        return malware_error, 422

    return None


def _visible_uploader_name(img: Image, viewer_profile: Profile | None) -> str:
    """The uploader's name as this viewer is allowed to see it.

    A photo can be visible while the identity behind it is not: a profile that
    has restricted who may see it is masked everywhere else it is named - the
    external API's ``owner_slug``, wiki edit attribution - and this gallery was
    printing ``profile.username`` straight off the row.

    Args:
        img: The photo.
        viewer_profile: Who is looking, or None for an anonymous request.

    Returns:
        The username, or the masked placeholder when the viewer may not see the
        uploader's identity. Empty string when the photo has no uploader.
    """
    from urbanlens.dashboard.services.profile.identity_visibility import DEFAULT_MASKED_PLACEHOLDER

    if img.profile is None:
        return ""
    if viewer_profile is not None and img.profile_id == viewer_profile.pk:
        return img.profile.username
    # `can_view_profile` rather than the fuller `resolve_visible_identity`: the
    # answer wanted here is only the name, and that helper also builds an avatar
    # and a profile URL, which is work this caller throws away - and a reverse()
    # a caller holding an unsaved profile cannot satisfy.
    return img.profile.username if img.profile.can_view_profile(viewer_profile) else DEFAULT_MASKED_PLACEHOLDER


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
        lightbox, and the two flags the pin gallery's delete prompt reads.
    """
    from urbanlens.dashboard.models.images.model import ImageSource

    return {
        "id": img.pk,
        "url": request.build_absolute_uri(img.image.url),
        "caption": img.caption or "",
        "latitude": float(img.latitude) if img.latitude is not None else None,
        "longitude": float(img.longitude) if img.longitude is not None else None,
        "uploader": _visible_uploader_name(img, viewer_profile),
        "is_mine": viewer_profile is not None and img.profile_id == viewer_profile.pk,
        "author": img.author or "",
        "source_url": img.source_url or "",
        "copyright": img.copyright or "",
        "taken_at": img.taken_at.isoformat() if img.taken_at else None,
        # What the pin gallery's delete prompt needs to know: whether removing
        # this photo from a pin would also take it off a community wiki, and
        # whether withdrawing it from there is even the owner's to do.
        "on_wiki": img.wiki_id is not None,
        "uploaded": img.source == ImageSource.UPLOAD,
    }


def delete_stored_file(image: Any, *, also_deleting: Collection[int] = ()) -> bool:
    """Remove an image's stored file, unless another row still points at it.

    Sharing a pin copies its photos by reusing the *same* storage key rather than
    duplicating the bytes (see ``services.sharing.pin_sharing.create_pin_from_share``),
    so one file can back several ``Image`` rows. Deleting the file whenever the first
    of those rows goes leaves everyone else's copy pointing at nothing - a broken
    photo, with no error anywhere to explain it.

    The file is still removed as soon as the last row referencing it goes, so this
    does not trade a broken photo for a storage leak.

    Args:
        image: The ``Image`` whose stored file should go.
        also_deleting: Primary keys of other rows being deleted in the same
            operation. They must not count as references, or a bulk delete would
            never remove anything.

    Returns:
        True when the file was deleted, False when another row still needs it (or
        there was no file).
    """
    from urbanlens.dashboard.models.images.model import Image as ImageModel

    name = image.image.name if image.image else ""
    if not name:
        return False

    still_referenced = ImageModel.objects.filter(image=name).exclude(pk__in=[image.pk, *also_deleting]).exists()
    if still_referenced:
        logger.debug("Keeping stored file %s: another image row still references it", name)
        return False

    image.image.delete(save=False)
    return True


def detach_image_from_pin(image: Any) -> None:
    """Remove ``image`` from its pin - delete the row outright only if nothing
    else still needs it.

    ``wiki_creation._seed_photos`` and ``PinGalleryBulkView``'s "send to wiki"
    action both repoint an existing pin photo's ``wiki`` FK rather than copying
    the row, so one ``Image`` can serve a pin and a wiki at once. A per-photo
    delete triggered from the pin side must not destroy the wiki's copy just
    because it shares the row - this mirrors the FK's own ``on_delete=SET_NULL``
    behavior for whole-pin deletion, applied to a single explicit delete too.

    Args:
        image: The ``Image`` to remove from its pin.
    """
    if image.wiki_id is not None:
        image.pin = None
        image.save(update_fields=["pin", "updated"])
        return
    delete_stored_file(image)
    image.delete()


def detach_image_from_wiki(image: Any) -> None:
    """The wiki-side mirror of ``detach_image_from_pin``.

    Args:
        image: The ``Image`` to remove from its wiki.
    """
    if image.pin_id is not None:
        image.wiki = None
        image.save(update_fields=["wiki", "updated"])
        return
    delete_stored_file(image)
    image.delete()
