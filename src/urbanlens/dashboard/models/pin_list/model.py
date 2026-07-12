"""PinList models - named, ordered collections of a profile's Pins.

A PinList can be plain (pins added/removed only by explicit user action) or
"smart" (``is_smart=True``), in which case it auto-includes pins matching a
saved filter (``smart_filter``, same JSON shape as ``SavedFilter.criteria``)
and/or falling inside a drawn boundary polygon (``smart_boundary``). See
``dashboard.services.pin_list_membership`` for the matching/sync logic and
``dashboard.models.pin_list.signals`` for the Pin-save hook that keeps
smart-list membership current.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.contrib.gis.db.models import MultiPolygonField
from django.db.models import CASCADE, SET_NULL, BooleanField, CharField, ForeignKey, Index, IntegerField, JSONField, TextField
from django.db.models.constraints import UniqueConstraint

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.services.text_limits import MAX_PIN_LIST_DESCRIPTION_LENGTH

if TYPE_CHECKING:
    from django.db.models import Manager as DjangoManager

logger = logging.getLogger(__name__)


class PinList(abstract.FrontendDashboardModel):
    """A profile's named, ordered collection of their own Pins."""

    profile = ForeignKey("dashboard.Profile", on_delete=CASCADE, related_name="pin_lists")
    name = CharField(max_length=100)
    description = TextField(blank=True, default="", max_length=MAX_PIN_LIST_DESCRIPTION_LENGTH)

    is_smart = BooleanField(default=False)
    # Same JSON shape as SavedFilter.criteria - see dashboard.services.filter_criteria.
    smart_filter = JSONField(null=True, blank=True)
    smart_boundary = MultiPolygonField(geography=True, srid=4326, null=True, blank=True)

    markup_map = ForeignKey(
        "dashboard.MarkupMap",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="pin_list",
    )

    if TYPE_CHECKING:
        items: DjangoManager[PinListItem]

    def __str__(self) -> str:
        return self.name

    @property
    def pin_count(self) -> int:
        """Number of pins currently on this list."""
        return self.items.count()

    class Meta(abstract.FrontendDashboardModel.Meta):
        db_table = "dashboard_pin_lists"
        ordering = ["-updated"]
        constraints = [UniqueConstraint(fields=["profile", "name"], name="uq_pin_list_profile_name")]
        indexes = [Index(fields=["profile"], name="idxdb_pinlist_profile")]


class PinListItem(abstract.DashboardModel):
    """A single Pin's membership in a PinList, with display order and provenance."""

    ADDED_MANUAL = "manual"
    ADDED_SMART_FILTER = "smart_filter"
    ADDED_BOUNDARY = "boundary"
    ADDED_VIA_CHOICES = [
        (ADDED_MANUAL, "Manually added"),
        (ADDED_SMART_FILTER, "Smart filter match"),
        (ADDED_BOUNDARY, "Inside boundary"),
    ]

    pin_list = ForeignKey(PinList, on_delete=CASCADE, related_name="items")
    pin = ForeignKey("dashboard.Pin", on_delete=CASCADE, related_name="list_memberships")
    order = IntegerField(default=0)
    added_via = CharField(max_length=20, choices=ADDED_VIA_CHOICES, default=ADDED_MANUAL)

    if TYPE_CHECKING:
        pin_list_id: int
        pin_id: int

    def __str__(self) -> str:
        return f"{self.pin_list_id}:{self.pin_id}"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_pin_list_items"
        ordering = ["order", "created"]
        constraints = [UniqueConstraint(fields=["pin_list", "pin"], name="uq_pin_list_item")]
        indexes = [
            Index(fields=["pin_list"], name="idxdb_pli_list"),
            Index(fields=["pin"], name="idxdb_pli_pin"),
        ]
