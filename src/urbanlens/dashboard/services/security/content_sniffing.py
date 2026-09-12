"""Magic-byte content-type sniffing for user uploads."""

from __future__ import annotations

from typing import IO

import filetype

from urbanlens.dashboard.models.images.model import MediaKind

# filetype's own per-format extensions, bucketed into our three MediaKinds.
# Anything filetype doesn't recognize (plain text, .docx/.pptx zip variants it can't always
# fingerprint, etc.) falls through to None in sniff_media_kind() - callers treat that as "no
# signature to check", not an automatic reject, since not every legitimate document format has one.
_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp", "heic", "heif", "bmp", "tif", "tiff", "avif"}
_VIDEO_EXTENSIONS = {"mp4", "mov", "avi", "mkv", "webm", "m4v", "flv", "wmv"}
_DOCUMENT_EXTENSIONS = {"pdf"}

#: What ``filetype`` calls the formats above, which is not always what a user calls them.
#: Kept separate from :data:`_IMAGE_EXTENSIONS` because that set constrains the *filename* - and
#: therefore the Content-Type the file is later served with - while this one only has to agree with
#: the library.
_SNIFFED_IMAGE_EXTENSIONS = _IMAGE_EXTENSIONS | {"apng"}


def guess_media_kind_from_extension(filename: str) -> MediaKind | None:
    """Guess a file's claimed MediaKind from its filename extension alone.

    Args:
        filename: The file's name (path or bare name; only the extension is used).

    Returns:
        The guessed ``MediaKind``, or ``None`` if the extension isn't one of the recognized image/video/document extensions."""
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension in _IMAGE_EXTENSIONS:
        return MediaKind.PHOTO
    if extension in _VIDEO_EXTENSIONS:
        return MediaKind.VIDEO
    if extension in _DOCUMENT_EXTENSIONS:
        return MediaKind.DOCUMENT
    return None


def unsupported_image_extension_error(filename: str) -> str | None:
    """Reject a photo upload whose extension we would not serve as a passive image.

    Args:
        filename: The uploaded file's client-supplied name.

    Returns:
        A user-facing error message when the extension is not an allowed image extension, else None."""
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension in _IMAGE_EXTENSIONS:
        return None
    return "That image format isn't supported. Please upload a JPEG, PNG, GIF, WebP, HEIC, BMP, TIFF, or AVIF file."


def sniff_media_kind(file_obj: IO[bytes]) -> MediaKind | None:
    """Detect the real media kind of an uploaded file from its magic bytes.

    Args:
        file_obj: The uploaded file to sniff.

    Returns:
        The ``MediaKind`` the file's bytes actually match, or ``None`` if ``filetype`` doesn't recognize the format at all."""
    kind = filetype.guess(file_obj)
    if kind is None:
        return None
    extension = kind.extension.lower()
    if extension in _SNIFFED_IMAGE_EXTENSIONS:
        return MediaKind.PHOTO
    if extension in _VIDEO_EXTENSIONS:
        return MediaKind.VIDEO
    if extension in _DOCUMENT_EXTENSIONS:
        return MediaKind.DOCUMENT
    return None


def photo_is_not_an_image_error(file_obj: IO[bytes]) -> str | None:
    """Reject a photo upload whose bytes are not any image at all. :func:`content_type_mismatch_error` only fires on a *confirmed* mismatch - bytes that fingerprint as something else.

    Args:
        file_obj: The uploaded file.

    Returns:
        A user-facing error message when the bytes are not a recognisable image, else ``None``."""
    if sniff_media_kind(file_obj) == MediaKind.PHOTO:
        return None
    return "That file doesn't look like an image. Please upload a JPEG, PNG, GIF, WebP, HEIC, BMP, TIFF, or AVIF file."


def content_type_mismatch_error(file_obj: IO[bytes], declared_media_type: MediaKind) -> str | None:
    """Reject an upload whose actual bytes don't match its declared kind.

    Args:
        file_obj: The uploaded file.
        declared_media_type: The ``MediaKind`` the caller classified the upload as, based on the client-supplied Content-Type/extension.

    Returns:
        A user-facing error message on a confirmed mismatch (e.g. a ``.jpg``-named file whose bytes are actually an executable), or ``None`` when the bytes match or the format isn't one ``filetype`` can fingerprint (in which case the declared type is trusted)."""
    sniffed = sniff_media_kind(file_obj)
    if sniffed is None or sniffed == declared_media_type:
        return None
    return "This file's contents don't match its file type - it may be mislabeled or corrupted, so it wasn't uploaded."
