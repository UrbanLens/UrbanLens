"""Photo keyword generation pipeline.

Runs entirely in the background (a Celery task enqueued after every upload's
``process_image_upload``) so uploads are never slowed down. Providers are
contributed by plugins via ``UrbanLensPlugin.get_photo_keyword_providers()``;
each enabled provider stores its own keywords in ``ImageKeyword`` rows
attributed to its slug, so multiple keywording strategies coexist and can be
regenerated independently. Keywords feed the global search's photo provider.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import io
import logging
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from urbanlens.dashboard.models.images.model import Image

logger = logging.getLogger(__name__)

#: Longest edge (px) of the downscaled copy sent to AI/classifier providers.
AI_IMAGE_MAX_DIMENSION = 512
#: Keywords stored per provider per image; extras are dropped by confidence.
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

    Contributed by plugins via
    :meth:`~urbanlens.dashboard.plugins.base.UrbanLensPlugin.get_photo_keyword_providers`.
    Providers run in the background per uploaded image; each stores its own
    ``ImageKeyword`` rows attributed to :attr:`slug`.

    Attributes:
        slug: Stable identifier stored on ``ImageKeyword.source``.
        label: Human-readable name for logs and admin surfaces.
    """

    slug: ClassVar[str] = ""
    label: ClassVar[str] = ""

    def is_available_for(self, image: Image) -> bool:
        """Whether this provider should run for this image's uploader.

        The pipeline already checks the uploader's ``generate_photo_keywords``
        setting; override this for provider-specific gates (subscription
        features, configured credentials, per-profile AI toggles).

        Args:
            image: The freshly uploaded image (``profile`` is populated).

        Returns:
            True when the provider can and may run.
        """
        return True

    @abstractmethod
    def generate(self, image: Image) -> list[KeywordResult]:
        """Produce keywords for one image.

        Args:
            image: The image to keyword; read its bytes via ``image.image``.

        Returns:
            Keyword candidates (normalization/dedup happens in the pipeline).
        """
        raise NotImplementedError


def downscaled_jpeg_bytes(image: Image, max_dimension: int = AI_IMAGE_MAX_DIMENSION) -> bytes | None:
    """Produce a small JPEG copy of an image for AI/classifier calls.

    Never sends full-resolution uploads to external services: the copy is
    capped at ``max_dimension`` on its longest edge and re-encoded as a
    quality-80 JPEG.

    Args:
        image: The Image row whose stored file to downscale.
        max_dimension: Longest-edge cap in pixels.

    Returns:
        JPEG bytes, or None when the stored file is missing or unreadable.
    """
    from PIL import Image as PILImage

    if not image.image:
        return None
    try:
        with image.image.open("rb") as stored_file:
            img: PILImage.Image = PILImage.open(stored_file)
            img.load()
    except (OSError, ValueError) as exc:
        logger.warning("Could not read image %s for keyword downscale: %s", image.pk, exc)
        return None

    img.thumbnail((max_dimension, max_dimension), PILImage.Resampling.LANCZOS)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=80)
    return buffer.getvalue()


def normalize_keywords(candidates: list[KeywordResult]) -> list[KeywordResult]:
    """Clean and deduplicate provider output before storage.

    Lowercases, trims punctuation/whitespace, drops empties and over-long
    strings (those are sentences, not tags), dedupes keeping the highest
    confidence, and caps the list at ``MAX_KEYWORDS_PER_SOURCE``.

    Args:
        candidates: Raw provider output.

    Returns:
        Normalized keywords, highest confidence first.
    """
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

    Skips entirely when the uploader turned off ``generate_photo_keywords``.
    Each provider is isolated: one failing provider is logged and skipped
    without affecting the others. A provider's previous keywords for this
    image are replaced by its fresh run.

    Args:
        image_id: PK of the image to keyword.

    Returns:
        Mapping of provider slug to number of keywords stored (for logs/tests).
    """
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
            ImageKeyword.objects.bulk_create(
                [ImageKeyword(image=image, source=provider.slug, keyword=result.keyword, confidence=result.confidence) for result in keywords],
            )
        counts[provider.slug] = len(keywords)
        if keywords:
            logger.info("Stored %d keyword(s) for image %s via '%s'", len(keywords), image_id, provider.slug)
    return counts
