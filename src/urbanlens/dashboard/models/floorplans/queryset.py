"""Version resolution for floorplans: which plan was in force on a date."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import Q

from urbanlens.dashboard.models.abstract.queryset import FrontendDashboardManager, FrontendDashboardQuerySet

if TYPE_CHECKING:
    import datetime

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

    def at(self, place: Place, on_date: datetime.date | None = None, *, profile=None, community: bool = False) -> Floorplan | None:
        """The plan in force on a date, or the most current when no date given.

        A version applies from its ``valid_from`` until the next dated
        version; the undated original applies from the beginning of time. So
        the answer is simply the latest version not after the date.

        Args:
            place: The building.
            on_date: The date to resolve at; None means "now".
            profile: Restrict to this profile's own *personal* plans. Callers
                serving a user should always pass one - a plan records door,
                lock and key detail, which is exactly the kind of thing that
                must not leak to everyone who happens to pin the same
                building.
            community: Look at published (wiki-owned) plans instead. The
                caller is responsible for having checked the wiki's own
                visibility rule.

        Returns:
            The floorplan in force, or None when there is none at all (the
            common case, and deliberately cheap: one indexed query).
        """
        from django.db.models import F

        versions = self.filter(place=place)
        if community:
            versions = versions.filter(wiki__isnull=False)
        elif profile is not None:
            # A personal plan only: a published one answers through the
            # community path, which checks the wiki's own visibility rule.
            versions = versions.filter(profile=profile, wiki__isnull=True)
        if on_date is not None:
            versions = versions.filter(Q(valid_from__isnull=True) | Q(valid_from__lte=on_date))
        # Ties broken by creation, so two versions dated the same day resolve
        # to one deterministic answer rather than whichever the planner found.
        return versions.order_by(F("valid_from").desc(nulls_last=True), "-created").first()


class FloorplanManager(FrontendDashboardManager.from_queryset(FloorplanQuerySet)):
    """Manager exposing :class:`FloorplanQuerySet`."""
