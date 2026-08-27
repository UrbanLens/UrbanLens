"""Wiki queryset and manager."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self

from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Q

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY, KIND_TAG

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki

logger = logging.getLogger(__name__)


class WikiQuerySet(abstract.VersionedQuerySet, abstract.PublicDashboardQuerySet):
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
        return self.filter(labels__name=category, labels__kind=KIND_CATEGORY)

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
                query &= Q(labels__name__in=[tag], labels__kind=KIND_TAG)
        return self.filter(query)


class WikiManager(abstract.PublicDashboardManager.from_queryset(WikiQuerySet)):
    """Manager for Wiki.

    Every pinned Location gets a page automatically
    (``tasks.ensure_wiki_for_location``), published from the moment it exists
    and filled in by background enrichment. There is no draft state and no
    create action; what a person contributes to a page is a separate, explicit
    act (``services.wiki.wiki_share``).

    Use ``get_for_location`` for "does this place have a page yet?".
    """

    def existing_for_location(self, location: Location | None) -> Wiki | None:
        """The Wiki describing what this Location stands on, draft or official.

        Checks the Location's own row first, then the *place* it resolved onto.
        The second lookup is the dedup that matters: two people pinning
        opposite ends of one property get two Locations, and without it they
        would get two community pages for one real-world thing.

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
            pass
        if location.place_id is None:
            return None
        return self.filter(place_id=location.place_id).first()

    def get_for_location(self, location: Location | None) -> Wiki | None:
        """Return the Location's Wiki, or None when it has none yet.

        Identical to :meth:`existing_for_location`, and kept because it is the
        name most call sites use. The two used to differ: a wiki was born as an
        invisible draft and this method filtered those out, so "does a wiki
        exist" and "is there one to show" were different questions. Wikis are
        published on creation now, and there is one question again.

        Args:
            location: The shared Location to look up (None-safe).

        Returns:
            The Wiki, or None.
        """
        return self.existing_for_location(location)

    def _placeholder_name(self, location: Location) -> str:
        """The fallback wiki name for a location with no official name."""
        return f"Unnamed Location in {location.area_label}" if location.area_label else "Unnamed Location"

    def get_or_create_for_location(self, location: Location, defaults: dict | None = None) -> tuple[Wiki, bool]:
        """Return the Wiki for a Location, creating it if absent.

        The one creation path. Called by ``tasks.ensure_wiki_for_location``
        when a pin gains a shared Location, and by the enrichment paths that
        need somewhere to write. Everything else should use
        ``get_for_location``, which never creates - a wiki appearing as a side
        effect of viewing or editing other content is a bug.

        Args:
            location: The shared Location to attach the wiki to.
            defaults: Optional field overrides for the created Wiki. A ``name``
                key wins over the location's ``official_name`` fallback.

        Returns:
            Tuple of (Wiki, created).
        """
        if (existing := self.existing_for_location(location)) is not None:
            return existing, False

        defaults = dict(defaults or {})
        name = defaults.pop("name", None) or location.official_name or self._placeholder_name(location)
        wiki = self.create(location=location, place_id=location.place_id, name=name, **defaults)
        return wiki, True
