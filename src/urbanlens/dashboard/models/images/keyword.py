"""ImageKeyword - searchable keywords attached to an uploaded photo.
Keywords are produced by photo-keyword plugins (see ``dashboard.services.photos.photo_keywords``).
Each plugin stores its own rows, attributed via ``source`` (the plugin slug), so multiple keywording strategies - embedded XMP/IPTC tags, AI vision descriptions, content classifiers - can coexist and be regenerated independently.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, CharField, DateTimeField, FloatField, ForeignKey, Index, PositiveSmallIntegerField, Q, UniqueConstraint

from urbanlens.dashboard.models import abstract

#: Longest keyword persisted; longer candidates are discarded, not truncated,
#: since a keyword that long is almost certainly a sentence, not a tag.
MAX_KEYWORD_LENGTH = 100


class ImageKeyword(abstract.DashboardModel):
    """One searchable keyword for one photo, attributed to the plugin that produced it."""

    keyword = CharField(max_length=MAX_KEYWORD_LENGTH, db_index=True)
    # Plugin slug (e.g. "photo_keywords_metadata", "photo_keywords_ai_vision").
    # Regeneration replaces only rows matching its own source.
    source = CharField(max_length=50)
    # Provider-reported confidence in [0, 1], when the provider scores its
    # keywords (classifiers do; embedded-metadata tags don't).
    confidence = FloatField(null=True, blank=True)

    image = ForeignKey(
        "dashboard.Image",
        on_delete=CASCADE,
        related_name="keywords",
    )

    if TYPE_CHECKING:
        image_id: int

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_image_keywords"
        constraints = [
            UniqueConstraint(fields=["image", "source", "keyword"], name="uniq_image_keyword_per_source"),
        ]
        indexes = [Index(fields=["source"], name="idx_image_keyword_source")]

    def __str__(self) -> str:
        return f"ImageKeyword({self.image_id}: {self.keyword!r} via {self.source})"


class ImageKeywordRetry(abstract.DashboardModel):
    """A keyword source that did not answer for a photo, and when to ask it again (P323).

    Written by ``services.photos.photo_keywords.generate_keywords_for_image`` when a source did not answer, deleted
    once it does, and swept by ``services.photos.keyword_retry.sweep``.
    """

    image = ForeignKey(
        "dashboard.Image",
        on_delete=CASCADE,
        related_name="keyword_retries",
    )
    # The provider's slug, as on ``ImageKeyword.source``.
    source = CharField(max_length=50)
    # Failures held against this photo; an outage or a refusal is not one.
    attempts = PositiveSmallIntegerField(default=0, db_default=0)
    # When the source may next be asked; null once the photo is given up.
    retry_at = DateTimeField(null=True, blank=True)
    # When the source first failed for this photo, which the 30-day give-up counts from; null while it has only refused.
    first_failed_at = DateTimeField(null=True, blank=True)

    if TYPE_CHECKING:
        image_id: int

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_image_keyword_retries"
        constraints = [
            UniqueConstraint(fields=["image", "source"], name="uniq_image_keyword_retry"),
        ]
        indexes = [Index(fields=["source", "retry_at"], condition=Q(retry_at__isnull=False), name="idx_image_keyword_retry_due")]

    def __str__(self) -> str:
        return f"ImageKeywordRetry({self.image_id} via {self.source}, {self.attempts} attempt(s), due {self.retry_at})"
