"""ImageKeyword - searchable keywords attached to an uploaded photo.

Keywords are produced by photo-keyword plugins (see
``dashboard.services.photo_keywords``). Each plugin stores its own rows,
attributed via ``source`` (the plugin slug), so multiple keywording
strategies - embedded XMP/IPTC tags, AI vision descriptions, content
classifiers - can coexist and be regenerated independently.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, CharField, FloatField, ForeignKey, Index, UniqueConstraint

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
