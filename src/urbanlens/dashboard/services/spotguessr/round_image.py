"""Serving a round's photo to a client that must not learn where it was taken."""

from __future__ import annotations

import io
import logging
from typing import TYPE_CHECKING

from PIL import Image as PILImage

if TYPE_CHECKING:
    from urbanlens.dashboard.models.images.model import Image

logger = logging.getLogger(__name__)

#: Formats served back in their original encoding. Anything else is re-encoded
#: as JPEG: an exotic container is not worth carrying a decoder-specific
#: metadata path for, and every mode that shows a photo shows a photograph.
_PRESERVED_FORMATS: dict[str, str] = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
    "GIF": "image/gif",
}

_FALLBACK_FORMAT = "JPEG"
_FALLBACK_CONTENT_TYPE = "image/jpeg"

#: Quality for a re-encode. High enough that the re-save is visually lossless at
#: the sizes a round is displayed at - the player is judging a place from this
#: image, so degrading it would change the difficulty of the game itself.
_JPEG_QUALITY = 92


class RoundImageUnavailableError(Exception):
    """The round's photo could not be read or decoded."""


def stripped_round_image(image: Image) -> tuple[bytes, str]:
    """Return one round photo's bytes with every metadata block removed.
    No ``exif=``, ``pnginfo=``, XMP or IPTC argument is passed to the save, so none of it survives - including tags this code has never heard of.

    Args:
        image: The ``Image`` row the round showed.

    Returns:
        A ``(bytes, content_type)`` pair ready to put in an HTTP response.

    Raises:
        RoundImageUnavailableError: The row has no stored file, or the file cannot be opened or decoded as an image."""
    if not image.image:
        raise RoundImageUnavailableError("This round's photo has no stored file.")

    try:
        with image.image.open("rb") as stored_file:
            opened = PILImage.open(stored_file)
            source_format = (opened.format or "").upper()
            # Force the pixels into memory before the file handle closes -
            # PIL is lazy, and saving afterwards would raise on a closed file.
            opened.load()
    except Exception as exc:
        logger.info("SpotGuessr round image %s could not be read: %s", image.pk, exc)
        raise RoundImageUnavailableError("This round's photo could not be read.") from exc

    target_format = source_format if source_format in _PRESERVED_FORMATS else _FALLBACK_FORMAT
    content_type = _PRESERVED_FORMATS.get(target_format, _FALLBACK_CONTENT_TYPE)

    decoded: PILImage.Image = opened
    save_kwargs: dict[str, object] = {}
    if target_format == "JPEG":
        # JPEG has no alpha channel; a PNG/WEBP source falling back to it would
        # otherwise raise rather than serve.
        if decoded.mode not in ("RGB", "L"):
            decoded = decoded.convert("RGB")
        save_kwargs.update(quality=_JPEG_QUALITY, optimize=True)

    buffer = io.BytesIO()
    try:
        decoded.save(buffer, format=target_format, **save_kwargs)
    except Exception as exc:
        logger.info("SpotGuessr round image %s could not be re-encoded: %s", image.pk, exc)
        raise RoundImageUnavailableError("This round's photo could not be prepared.") from exc

    return buffer.getvalue(), content_type
