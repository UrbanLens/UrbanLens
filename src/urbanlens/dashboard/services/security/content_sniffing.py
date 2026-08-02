"""Magic-byte content-type sniffing for user uploads.

``PhotoUploadView`` classifies uploads into a :class:`MediaKind` by trusting
the client-supplied ``Content-Type`` header and the filename extension -
either can be spoofed by a malicious or simply misbehaving client, letting a
mislabeled file sail through size/quota checks under the wrong kind. This
re-derives the file's *actual* type from its own bytes (via ``filetype``,
which matches known magic-byte signatures) and cross-checks it against what
the client claimed.
"""

from __future__ import annotations

from typing import IO

import filetype

from urbanlens.dashboard.models.images.model import MediaKind

# filetype's own per-format extensions, bucketed into our three MediaKinds.
# Anything filetype doesn't recognize (plain text, .docx/.pptx zip variants
# it can't always fingerprint, etc.) falls through to None in
# sniff_media_kind() - callers treat that as "no signature to check", not an
# automatic reject, since not every legitimate document format has one.
_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp", "heic", "heif", "bmp", "tiff", "avif"}
_VIDEO_EXTENSIONS = {"mp4", "mov", "avi", "mkv", "webm", "m4v", "flv", "wmv"}
_DOCUMENT_EXTENSIONS = {"pdf"}


def guess_media_kind_from_extension(filename: str) -> MediaKind | None:
    """Guess a file's claimed MediaKind from its filename extension alone.

    For places with no client-supplied Content-Type to trust at all - e.g. a
    file extracted from a data-export archive during re-import - the
    extension is the only signal available for what the file *claims* to be,
    to then cross-check against :func:`sniff_media_kind`'s magic-byte read of
    what it *actually* is via :func:`content_type_mismatch_error`.

    Args:
        filename: The file's name (path or bare name; only the extension is used).

    Returns:
        The guessed ``MediaKind``, or ``None`` if the extension isn't one of
        the recognized image/video/document extensions.
    """
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension in _IMAGE_EXTENSIONS:
        return MediaKind.PHOTO
    if extension in _VIDEO_EXTENSIONS:
        return MediaKind.VIDEO
    if extension in _DOCUMENT_EXTENSIONS:
        return MediaKind.DOCUMENT
    return None


def sniff_media_kind(file_obj: IO[bytes]) -> MediaKind | None:
    """Detect the real media kind of an uploaded file from its magic bytes.

    Args:
        file_obj: The uploaded file to sniff. Its read position is left
            unchanged (``filetype`` only peeks at the first few KB and
            restores the original position itself).

    Returns:
        The ``MediaKind`` the file's bytes actually match, or ``None`` if
        ``filetype`` doesn't recognize the format at all.
    """
    kind = filetype.guess(file_obj)
    if kind is None:
        return None
    extension = kind.extension.lower()
    if extension in _IMAGE_EXTENSIONS:
        return MediaKind.PHOTO
    if extension in _VIDEO_EXTENSIONS:
        return MediaKind.VIDEO
    if extension in _DOCUMENT_EXTENSIONS:
        return MediaKind.DOCUMENT
    return None


def content_type_mismatch_error(file_obj: IO[bytes], declared_media_type: MediaKind) -> str | None:
    """Reject an upload whose actual bytes don't match its declared kind.

    Args:
        file_obj: The uploaded file.
        declared_media_type: The ``MediaKind`` the caller classified the
            upload as, based on the client-supplied Content-Type/extension.

    Returns:
        A user-facing error message on a confirmed mismatch (e.g. a
        ``.jpg``-named file whose bytes are actually an executable), or
        ``None`` when the bytes match or the format isn't one ``filetype``
        can fingerprint (in which case the declared type is trusted).
    """
    sniffed = sniff_media_kind(file_obj)
    if sniffed is None or sniffed == declared_media_type:
        return None
    return "This file's contents don't match its file type - it may be mislabeled or corrupted, so it wasn't uploaded."
