"""Photo keyword generation pipeline.
Runs entirely in the background (a Celery task enqueued after every upload's ``process_image_upload``) so uploads are never slowed down."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
import logging
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from urbanlens.dashboard.models.images.model import Image

logger = logging.getLogger(__name__)

# The copy's dimensions and format belong to the writer that produces it -
# services.media.images.ANALYSIS_THUMBNAIL_MAX_DIMENSION.
# Nothing here needs the number: this module reads whatever the sandbox already wrote.
MAX_KEYWORDS_PER_SOURCE = 30


class KeywordSourceUnavailableError(RuntimeError):
    """A provider's source did not answer for this photo, so the keywords it stored before stand."""


class AnalysisCopyUnavailableError(KeywordSourceUnavailableError):
    """The photo's analysis copy is missing or unreadable, so no source was asked and its keywords stand."""


class KeywordOutcome(StrEnum):
    """What asking one provider about one photo came to."""

    #: The source answered; its keywords (perhaps none) replaced the photo's.
    ANSWERED = "answered"
    #: The source was asked and did not answer, or the provider failed; the photo's keywords stand.
    UNANSWERED = "unanswered"
    #: Refused for now before anything was sent: over a limit, or backed off. Nothing is held against the photo.
    BUSY = "busy"
    #: Refused for good here before anything was sent: switched off, or not called in this environment (D26).
    OFF = "off"
    #: The photo's analysis copy is missing or unreadable, so the source was not asked.
    NO_INPUT = "no_input"
    #: The provider does not run for this photo: its uploader's settings, the site's, or the provider's own needs.
    NOT_AVAILABLE = "not_available"


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
        label: Human-readable name for logs and admin surfaces.
        service_key: The rate-limiter service the provider calls, so the retry sweep can tell whether it may be
            asked here and now (``services.photos.keyword_retry``); empty for a provider that calls nothing."""

    slug: ClassVar[str] = ""
    label: ClassVar[str] = ""
    service_key: ClassVar[str] = ""

    def is_available_for(self, image: Image) -> bool:
        """Whether this provider should run for this image's uploader.

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

        Raises:
            KeywordSourceUnavailableError: The source did not answer, so the photo's keywords from it stand.
            RequestCancelledError: The call was refused before it was sent; nothing was asked.
        """
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


def require_analysis_jpeg_bytes(image: Image) -> bytes:
    """The analysis copy's bytes, for a provider that must not answer for a photo it could not see.

    Args:
        image: The Image row whose analysis copy to read.

    Returns:
        JPEG bytes.

    Raises:
        AnalysisCopyUnavailableError: The row has no analysis copy yet, or it could not be read. Returning no
            keywords instead would replace the ones the photo has with none (P322); the backfill that writes a
            missing copy enqueues keywording again.
    """
    data = analysis_jpeg_bytes(image)
    if data is None:
        raise AnalysisCopyUnavailableError(f"image {image.pk} has no readable analysis copy")
    return data


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


def run_keyword_provider(image: Image, provider: PhotoKeywordProvider) -> tuple[KeywordOutcome, int]:
    """Ask one provider about one photo, and store its keywords if it answers.

    A provider that does not answer leaves the photo's keywords from it as they are (P322). Nothing here records a
    retry; the callers decide what an outcome means for one (``services.photos.keyword_retry``).

    Args:
        image: The photo, with ``profile`` loaded.
        provider: The provider to ask.

    Returns:
        The outcome, and how many keywords were stored (0 unless it answered).
    """
    from django.db import transaction

    from urbanlens.dashboard.models.images.keyword import ImageKeyword
    from urbanlens.dashboard.services.core.rate_limiter import RequestCancelledError
    from urbanlens.dashboard.services.core.task_limits import SOFT_TIME_LIMIT_ERRORS

    try:
        if not provider.is_available_for(image):
            return KeywordOutcome.NOT_AVAILABLE, 0
        keywords = normalize_keywords(provider.generate(image))
    except SOFT_TIME_LIMIT_ERRORS:
        # Celery's SoftTimeLimitExceeded is an Exception: caught below, the task's soft limit would pass for this
        # source failing on this photo, and the task would carry on to its hard limit.
        raise
    except RequestCancelledError as exc:
        logger.info("Photo keyword provider '%s' was not sent for image %s: %s", provider.slug, image.pk, exc)
        return (KeywordOutcome.BUSY if exc.transient else KeywordOutcome.OFF), 0
    except AnalysisCopyUnavailableError as exc:
        logger.info("Photo keyword provider '%s' kept image %s's keywords: %s", provider.slug, image.pk, exc)
        return KeywordOutcome.NO_INPUT, 0
    except KeywordSourceUnavailableError as exc:
        logger.info("Photo keyword provider '%s' kept image %s's keywords: %s", provider.slug, image.pk, exc)
        return KeywordOutcome.UNANSWERED, 0
    except Exception:
        logger.exception("Photo keyword provider '%s' failed for image %s", provider.slug, image.pk)
        return KeywordOutcome.UNANSWERED, 0

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
    if keywords:
        logger.info("Stored %d keyword(s) for image %s via '%s'", len(keywords), image.pk, provider.slug)
    return KeywordOutcome.ANSWERED, len(keywords)


def generate_keywords_for_image(image_id: int) -> dict[str, int]:
    """Run every enabled photo-keyword provider for one uploaded image.
    Each provider is isolated: one failing provider is logged and skipped without affecting the others. A source that
    did not answer is recorded for the retry sweep (P323).

    Args:
        image_id: PK of the image to keyword.

    Returns:
        Mapping of provider slug to number of keywords stored (for logs/tests)."""
    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.plugins.registry import plugin_registry
    from urbanlens.dashboard.services.photos import keyword_retry

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
        outcome, stored = run_keyword_provider(image, provider)
        if outcome is KeywordOutcome.ANSWERED:
            counts[provider.slug] = stored
        keyword_retry.note_outcome(image, provider.slug, outcome)
    return counts
