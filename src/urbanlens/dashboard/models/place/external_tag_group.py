"""Admin-curated equivalence groups over the PlaceExternalTag vocabulary.

Different providers often describe the same real-world concept differently
(OSM's ``amenity=restaurant`` vs. Overture's ``building_subtype=restaurant``).
This module lets an admin mark two or more distinct ``(source, key, value)``
tags as meaning the same thing, and pick which one is shown when a Place
carries more than one member of the group. See
``services.locations.external_tag_groups`` for the resolution logic and
``docs/FEATURES.md`` for the feature overview.

Deliberately separate from ``PlaceExternalTag`` itself: a given tag tuple
repeats across many Places, and grouping/preference is a property of the tag,
not of any one Place's copy of it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import SET_NULL, BooleanField, CharField, ForeignKey, Index, Q, UniqueConstraint

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.place.external_tag import ExternalTagSource
from urbanlens.dashboard.models.place.queryset import ExternalTagGroupManager, ExternalTagVocabularyEntryManager


class ExternalTagGroup(abstract.DashboardModel):
    """A set of vocabulary entries considered the same real-world concept.

    Carries no fields beyond identity and timestamps - membership and which
    member is displayed both live on :class:`ExternalTagVocabularyEntry`.
    """

    objects = ExternalTagGroupManager()

    if TYPE_CHECKING:
        id: int

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_external_tag_groups"

    def __str__(self) -> str:
        return f"ExternalTagGroup {self.pk}"


class ExternalTagVocabularyEntry(abstract.DashboardModel):
    """One distinct ``(source, key, value)`` tag ever reported by a provider.

    Auto-registered (``get_or_create``) by ``PlaceExternalTag.sync_for_source``
    as new tags appear, and never auto-deleted - a tag going temporarily
    unseen on every Place shouldn't drop an admin's mapping decision, the
    same reasoning that keeps an unused ``Label`` alive.

    Attributes:
        source: Which provider reported this tag.
        key: The provider's tag/attribute name.
        value: The raw value.
        group: The equivalence group this tag belongs to, if an admin (or a
            "confirm suggested" action) has explicitly placed it in one.
            ``None`` means it falls back to default same-display-text
            matching against other ungrouped entries - see
            ``services.locations.external_tag_groups.default_group_key``.
        is_preferred: Whether this is the member shown when a Place carries
            more than one member of ``group``. Meaningless while ``group``
            is ``None``.
    """

    source = CharField(max_length=20, choices=ExternalTagSource.choices)
    key = CharField(max_length=100)
    value = CharField(max_length=255)
    group = ForeignKey(ExternalTagGroup, on_delete=SET_NULL, null=True, blank=True, related_name="members")
    is_preferred = BooleanField(default=False)

    objects = ExternalTagVocabularyEntryManager()

    if TYPE_CHECKING:
        id: int
        group_id: int | None

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_external_tag_vocabulary"
        ordering = ["source", "key", "value"]
        constraints = [
            UniqueConstraint(fields=["source", "key", "value"], name="external_tag_vocabulary_unique"),
            # Partial unique index: enforces "at most one preferred member per
            # group" without blocking any number of non-preferred rows.
            UniqueConstraint(fields=["group"], condition=Q(is_preferred=True), name="external_tag_vocabulary_one_preferred_per_group"),
        ]
        indexes = [
            Index(fields=["group"], name="idxdb_exttag_vocab_group"),
        ]

    def __str__(self) -> str:
        return f"{self.get_source_display()}: {self.key}={self.value}"
