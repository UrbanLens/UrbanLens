"""Photo keyword generation pipeline.
Runs entirely in the background (a Celery task enqueued after every upload's ``process_image_upload``) so uploads are never slowed down."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from urbanlens.dashboard.models.images.model import Image

logger = logging.getLogger(__name__)

# The copy's dimensions and format belong to the writer that produces it -
# services.media.images.ANALYSIS_THUMBNAIL_MAX_DIMENSION.
# Nothing here needs the number: this module reads whatever the sandbox already wrote.
MAX_KEYWORDS_PER_SOURCE = 30


@dataclass(frozen=True, slots=True)
class KeywordResult:
    """One keyword produced by a provider.

    Attributes:
        keyword: The keyword text (will be normalized before storage).
        confidence: Provider-reported confidence in [0, 1], when scored.
    """

    keyword: str
    confidence: float | None = None


class PhotoKeywordProvider(ABC):
    """One keywording strategy for uploaded photos.

    Attributes:
        slug: Stable identifier stored on ``ImageKeyword.source``.
        label: Human-readable name for logs and admin surfaces."""

    slug: ClassVar[str] = ""
    label: ClassVar[str] = ""

    def is_available_for(self, image: Image) -> bool:
        """Whether this provider should run for this image's uploader.

        Returns:
            True when the provider can and may run."""
        return True

    @abstractmethod
    def generate(self, image: Image) -> list[KeywordResult]:
        """Produce keywords for one image.

        Returns:
            Keyword candidates (normalization/dedup happens in the pipeline)."""
        raise NotImplementedError


def analysis_jpeg_bytes(image: Image) -> bytes | None:
    """Reads bytes; never parses them.

    Args:
        image: The Image row whose analysis copy to read.

    Returns:
        JPEG bytes, or None when the row has no analysis copy yet or the file cannot be read."""
    if not image.analysis_thumbnail:
        logger.info("Image %s has no analysis copy yet; skipping keyword generation until the backfill writes one", image.pk)
        return None
    try:
        with image.analysis_thumbnail.open("rb") as stored_file:
            data: bytes = stored_file.read()
    except (OSError, ValueError) as exc:
        logger.warning("Could not read the analysis copy for image %s: %s", image.pk, exc)
        return None
    return data or None


def normalize_keywords(candidates: list[KeywordResult]) -> list[KeywordResult]:
    """Clean and deduplicate provider output before storage.

    Args:
        candidates: Raw provider output.

    Returns:
        Normalized keywords, highest confidence first."""
    from urbanlens.dashboard.models.images.keyword import MAX_KEYWORD_LENGTH

    best: dict[str, KeywordResult] = {}
    for candidate in candidates:
        keyword = " ".join(candidate.keyword.lower().strip(" .,;:!?\"'()[]").split())
        if not keyword or len(keyword) > MAX_KEYWORD_LENGTH:
            continue
        existing = best.get(keyword)
        if existing is None or (candidate.confidence or 0) > (existing.confidence or 0):
            best[keyword] = KeywordResult(keyword=keyword, confidence=candidate.confidence)
    ordered = sorted(best.values(), key=lambda result: result.confidence or 0, reverse=True)
    return ordered[:MAX_KEYWORDS_PER_SOURCE]


def generate_keywords_for_image(image_id: int) -> dict[str, int]:
    """Run every enabled photo-keyword provider for one uploaded image.
    Each provider is isolated: one failing provider is logged and skipped without affecting the others.

    Args:
        image_id: PK of the image to keyword.

    Returns:
        Mapping of provider slug to number of keywords stored (for logs/tests)."""
    from django.db import transaction

    from urbanlens.dashboard.models.images.keyword import ImageKeyword
    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.plugins.registry import plugin_registry

    image = Image.objects.select_related("profile__user").filter(pk=image_id).first()
    if image is None or not image.image:
        logger.info("generate_keywords_for_image: image %s no longer exists", image_id)
        return {}
    if image.profile is not None and not image.profile.generate_photo_keywords:
        logger.debug("Photo keywords disabled for profile %s; skipping image %s", image.profile_id, image_id)
        return {}

    counts: dict[str, int] = {}
    for provider in plugin_registry.photo_keyword_providers():
        if not provider.slug:
            continue
        try:
            if not provider.is_available_for(image):
                continue
            keywords = normalize_keywords(provider.generate(image))
        except Exception:
            logger.exception("Photo keyword provider '%s' failed for image %s", provider.slug, image_id)
            continue

        with transaction.atomic():
            ImageKeyword.objects.filter(image=image, source=provider.slug).delete()
            # ignore_conflicts because the delete above does not isolate this from another worker
            # replacing the same provider's keywords.
            # The only caller is a Celery task, and Celery delivers at least once - a worker lost
            # mid-task has its message redelivered, so two runs for one image is ordinary rather
            ImageKeyword.objects.bulk_create(
                [ImageKeyword(image=image, source=provider.slug, keyword=result.keyword, confidence=result.confidence) for result in keywords],
                ignore_conflicts=True,
            )
        counts[provider.slug] = len(keywords)
        if keywords:
            logger.info("Stored %d keyword(s) for image %s via '%s'", len(keywords), image_id, provider.slug)
    return counts
