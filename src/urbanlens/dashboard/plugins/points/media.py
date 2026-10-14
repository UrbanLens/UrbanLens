"""Photos and labels: relevance scoring, label seeding and the media gallery."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.plugins.extensions import ActionHook, Choices, FilterHook, Registry

if TYPE_CHECKING:
    from urbanlens.dashboard.models.images.model import Image, ImageSourceTraits
    from urbanlens.dashboard.models.images.queryset import ImageQuerySet
    from urbanlens.dashboard.models.profile.model import Profile


PHOTO_LOCATED: ActionHook[[Image]] = ActionHook(
    "photos.located",
    "Fired when a processed photo has a location, so a relevance scorer can be handed it. Runs inside the upload-processing task; a callback should queue its own slow work.",
)

PHOTO_RELEVANCE_VOTED: ActionHook[[Image, Profile, bool]] = ActionHook(
    "photos.relevance_voted",
    "Fired when a profile explicitly marks a stored photo relevant (True) or not relevant (False) to its place. Clearing a vote does not fire it.",
)

PHOTO_RELEVANCE_SCORES: FilterHook[ImageQuerySet, []] = FilterHook(
    "photos.relevance_scores",
    "Annotates a photo queryset with ``relevance_confidence``: a float from 0 to 1, or null where the photo is unscored. Photos are ordered by it, best first, after the community's votes. Core annotates null where no callback does.",
)

IMAGE_SOURCES = Choices(
    "media.image_sources",
    "Where a photo can come from, as ``value -> label``. Core's are the ``ImageSource`` members; a plugin adds the sources it produces.",
)

IMAGE_SOURCE_TRAITS: Registry[str, ImageSourceTraits] = Registry(
    "media.image_source_traits",
    "What core does with a source beyond its label, keyed by ``ImageSource`` value: its Media-gallery panel keys and whether a VirusTotal scan may vet it.",
)

LABELS_SEEDED: ActionHook[[Profile]] = ActionHook(
    "labels.seeded",
    "Fired after a new profile's default labels were created in bulk, which sends no per-label save signals.",
)

__all__ = ["IMAGE_SOURCES", "IMAGE_SOURCE_TRAITS", "LABELS_SEEDED", "PHOTO_LOCATED", "PHOTO_RELEVANCE_SCORES", "PHOTO_RELEVANCE_VOTED"]
