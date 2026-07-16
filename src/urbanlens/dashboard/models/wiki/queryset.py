"""Wiki queryset and manager."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self

from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Q

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.wiki.model import Wiki

logger = logging.getLogger(__name__)


class WikiQuerySet(abstract.PublicDashboardQuerySet):
    """QuerySet for Wiki - the community-editable half of the place model.

    Filters here operate on community data (name, labels). For address/geo
    filtering use LocationQuerySet; for per-user filtering use PinQuerySet.
    """

    def root_wikis(self) -> Self:
        """Return only top-level wikis (excludes child wikis)."""
        return self.filter(parent_wiki__isnull=True)

    def child_wikis(self) -> Self:
        """Return only child wikis (community sub-markers nested under a parent wiki)."""
        return self.filter(parent_wiki__isnull=False)

    def by_category(self, category):
        return self.filter(labels__name=category, labels__kind="category")

    def by_name(self, name):
        return self.filter(name__icontains=name)

    def by_created_year(self, year):
        return self.filter(created__year=year)

    def by_updated_year(self, year):
        return self.filter(updated__year=year)

    def filter_by_criteria(self, criteria):
        query = Q()
        if criteria.get("date_added"):
            query &= Q(created__date=criteria["date_added"])
        if criteria.get("tags"):
            tags = criteria["tags"].split(",")
            for tag in tags:
                query &= Q(labels__name__in=[tag], labels__kind="tag")
        return self.filter(query)


class WikiManager(abstract.PublicDashboardManager.from_queryset(WikiQuerySet)):
    """Manager for Wiki.

    Wikis are only ever created explicitly by a user (the "Create community
    wiki" button on the pin detail page, via ``WikiCreationService``). Use
    ``get_for_location`` for the common "does this place have a wiki?" lookup.
    """

    def get_for_location(self, location: Location | None) -> Wiki | None:
        """Return the Wiki for a Location, or None when the place has no wiki yet.

        Args:
            location: The shared Location to look up (None-safe).

        Returns:
            The Wiki, or None.
        """
        if location is None:
            return None
        try:
            return location.wiki
        except ObjectDoesNotExist:
            return None

    def get_or_create_for_location(self, location: Location, defaults: dict | None = None) -> tuple[Wiki, bool]:
        """Return the Wiki for a Location, creating it if absent.

        Wikis are user-initiated: this must only be called from an explicit
        create action (``WikiCreationService``), never as a lazy side effect of
        viewing or editing other content - use ``get_for_location`` there.

        Args:
            location: The shared Location to attach the wiki to.
            defaults: Optional field overrides for the created Wiki. A ``name``
                key wins over the location's ``official_name`` fallback.

        Returns:
            Tuple of (Wiki, created).
        """
        try:
            return location.wiki, False
        except ObjectDoesNotExist:
            pass

        defaults = dict(defaults or {})
        placeholder = f"Unnamed Location in {location.area_label}" if location.area_label else "Unnamed Location"
        name = defaults.pop("name", None) or location.official_name or placeholder
        wiki = self.create(location=location, name=name, **defaults)
        return wiki, True
