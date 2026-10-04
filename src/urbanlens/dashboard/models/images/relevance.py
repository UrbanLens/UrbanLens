"""Per-user relevance marks for the Private Pin page's Media gallery.
The Media gallery (Wikimedia/Smithsonian/Yelp/Google Images/...) renders straight from each provider's live results (see ``services.pins.external_data``) rather than persisting an ``Image`` row per item, so "relevant"/"not relevant" can't hang off an FK to one.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from django.db.models import CASCADE, BooleanField, CharField, CheckConstraint, ForeignKey, Index, Q, UniqueConstraint

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.images.queryset import MediaRelevanceManager

#: Length of a ``media_item_key``, and the most ``MediaRelevance.item_key`` holds.
ITEM_KEY_LENGTH = 40


def media_item_key(url: str) -> str:
    """Stable, short identifier for a transient Media gallery item.

    Args:
        url: The item's ``url`` (its full-resolution image URL - the same
            field ``MediaProvider.get_media`` dedupes provider results on).

    Returns:
        A 40-character hex digest suitable for ``MediaRelevance.item_key``, the same with or without tracking parameters.
    """
    from urbanlens.dashboard.services.core.tracking_params import without_tracking_params

    return hashlib.sha1(without_tracking_params(url).encode("utf-8"), usedforsecurity=False).hexdigest()


class MediaRelevance(abstract.DashboardModel):
    """One user's relevance mark on one Media gallery item.

    ``is_relevant``: ``True`` (explicitly relevant - sorts first under
    "Relevant first"), ``False`` (not relevant - hidden by default), or the
    row simply doesn't exist (neutral/unmarked - shown, not prioritized).

    ``is_vote``: whether the mark counts toward the item's community score. A "remove from my results" mark is not a
    vote: it hides the item from this profile's pin pages only, so it is never ``is_relevant``.
    """

    profile = ForeignKey("dashboard.Profile", on_delete=CASCADE, related_name="media_relevance_marks")
    location = ForeignKey("dashboard.Location", on_delete=CASCADE, related_name="media_relevance_marks")
    source = CharField(max_length=30)
    item_key = CharField(max_length=ITEM_KEY_LENGTH)
    is_relevant = BooleanField()
    is_vote = BooleanField(default=True)

    objects = MediaRelevanceManager()

    if TYPE_CHECKING:
        profile_id: int
        location_id: int

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_media_relevance"
        constraints = [
            UniqueConstraint(fields=["profile", "location", "source", "item_key"], name="db_media_relevance_unique"),
            CheckConstraint(condition=Q(is_vote=True) | Q(is_relevant=False), name="db_media_relevance_private_hides"),
        ]
        indexes = [
            Index(fields=["profile", "location"], name="idxdb_medrel_profile_loc"),
        ]
