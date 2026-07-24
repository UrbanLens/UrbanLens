"""Photo selection for Photos-mode rounds.

See ``docs/designs/spotguessr.md`` ("Photo selection (Photos mode)") for why
there is deliberately no separate "opted into the game" gate yet - that
gate is UL-394's community submission pipeline.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

from urbanlens.dashboard.models.images.model import Image, ImageSource, MediaKind

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location


def candidate_image_for_location(location: Location, *, external_media_only: bool = False) -> Image | None:
    """Pick a photo to show for ``location``, or None if it has no eligible photo.

    Args:
        location: The round's answer location.
        external_media_only: When True (``config.external_media_only``),
            excludes plain personal uploads (``ImageSource.UPLOAD``),
            keeping only externally-sourced media (Wikimedia, Google
            Images, Smithsonian, etc.).
    """
    images = Image.objects.filter(location=location, media_type=MediaKind.PHOTO)
    if external_media_only:
        images = images.exclude(source=ImageSource.UPLOAD)
    candidates = list(images)
    if not candidates:
        return None
    return random.choice(candidates)  # noqa: S311 - game content selection, not security-sensitive
