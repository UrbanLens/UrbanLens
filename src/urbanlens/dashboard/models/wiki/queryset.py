"""Wiki queryset and manager."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Q

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.wiki.model import Wiki

logger = logging.getLogger(__name__)


class WikiQuerySet(abstract.PublicDashboardQuerySet):
    """QuerySet for Wiki - the community-editable half of the place model.

    Filters here operate on community data (name, badges). For address/geo
    filtering use LocationQuerySet; for per-user filtering use PinQuerySet.
    """

    def by_category(self, category):
        return self.filter(badges__name=category, badges__kind="category")

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
                query &= Q(badges__name__in=[tag], badges__kind="tag")
        return self.filter(query)


class WikiManager(abstract.PublicDashboardManager.from_queryset(WikiQuerySet)):
    """Manager for Wiki. Use get_or_create_for_location to lazily create a page for a Location."""

    def get_or_create_for_location(self, location: Location, defaults: dict | None = None) -> tuple[Wiki, bool]:
        """Return the Wiki for a Location, creating it lazily if absent.

        A Wiki is created on demand (backfill-only-where-used): pin creation does
        not eagerly create wikis, so the first time community content is added or
        the wiki page is opened we materialise one here.

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
        name = defaults.pop("name", None) or location.official_name or "Unnamed Location"
        wiki = self.create(location=location, name=name, **defaults)
        return wiki, True
