"""Version resolution for floorplans: which plan was in force on a date."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from django.db.models import Q

from urbanlens.dashboard.models.abstract.queryset import FrontendDashboardManager, FrontendDashboardQuerySet

if TYPE_CHECKING:
    from urbanlens.dashboard.models.floorplans.model import Floorplan
    from urbanlens.dashboard.models.place.model import Place


class FloorplanQuerySet(FrontendDashboardQuerySet):
    """Query helpers for versioned floorplan documents."""

    def for_place(self, place: Place) -> FloorplanQuerySet:
        """Every version of this building's plan, oldest first.

        Args:
            place: The building.

        Returns:
            Versions ordered by ``valid_from`` with the undated original
            first.
        """
        from django.db.models import F

        return self.filter(place=place).order_by(F("valid_from").asc(nulls_first=True))

    def at(self, place: Place, on_date: datetime.date | None = None) -> Floorplan | None:
        """The plan in force on a date, or the most current when no date given.

        A version applies from its ``valid_from`` until the next dated
        version; the undated original applies from the beginning of time. So
        the answer is simply the latest version not after the date.

        Args:
            place: The building.
            on_date: The date to resolve at; None means "now".

        Returns:
            The floorplan in force, or None when the building has none at all
            (the common case, and deliberately cheap: one indexed query).
        """
        from django.db.models import F

        versions = self.filter(place=place)
        if on_date is not None:
            versions = versions.filter(Q(valid_from__isnull=True) | Q(valid_from__lte=on_date))
        return versions.order_by(F("valid_from").desc(nulls_last=True)).first()


class FloorplanManager(FrontendDashboardManager.from_queryset(FloorplanQuerySet)):
    """Manager exposing :class:`FloorplanQuerySet`."""
