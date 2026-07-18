"""Per-user relevance marks for the pin detail page's Media gallery.

The Media gallery (Wikimedia/Smithsonian/Yelp/Google Images/...) renders
straight from each provider's live results (see ``services.external_data``)
rather than persisting an ``Image`` row per item, so "relevant"/"not relevant"
can't hang off an FK to one. This model keys a mark to the stable identity of
a transient item instead: which Location it belongs to, which provider
produced it, and a hash of its URL.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from django.db.models import CASCADE, BooleanField, CharField, ForeignKey, Index, UniqueConstraint

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.images.queryset import MediaRelevanceManager

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.profile.model import Profile


def media_item_key(url: str) -> str:
    """Stable, short identifier for a transient Media gallery item.

    Args:
        url: The item's ``url`` (its full-resolution image URL - the same
            field ``MediaProvider.get_media`` dedupes provider results on).

    Returns:
        A 40-character hex digest suitable for ``MediaRelevance.item_key``.
    """
    return hashlib.sha1(url.encode("utf-8"), usedforsecurity=False).hexdigest()


class MediaRelevance(abstract.DashboardModel):
    """One user's relevance mark on one Media gallery item.

    ``is_relevant``: ``True`` (explicitly relevant - sorts first under
    "Relevant first"), ``False`` (not relevant - hidden by default), or the
    row simply doesn't exist (neutral/unmarked - shown, not prioritized).
    """

    profile = ForeignKey("dashboard.Profile", on_delete=CASCADE, related_name="media_relevance_marks")
    location = ForeignKey("dashboard.Location", on_delete=CASCADE, related_name="media_relevance_marks")
    source = CharField(max_length=30)
    item_key = CharField(max_length=40)
    is_relevant = BooleanField()

    objects = MediaRelevanceManager()

    if TYPE_CHECKING:
        profile_id: int
        location_id: int

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_media_relevance"
        constraints = [
            UniqueConstraint(fields=["profile", "location", "source", "item_key"], name="db_media_relevance_unique"),
        ]
        indexes = [
            Index(fields=["profile", "location"], name="idxdb_medrel_profile_loc"),
        ]
