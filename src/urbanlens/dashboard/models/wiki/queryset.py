"""Wiki queryset and manager."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self

from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError, transaction

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.abstract.tree import TreeQuerySetMixin

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.wiki.model import Wiki

logger = logging.getLogger(__name__)


class WikiQuerySet(abstract.VersionedQuerySet, abstract.PublicDashboardQuerySet["Wiki"], TreeQuerySetMixin):
    """QuerySet for Wiki - the community-editable half of the place model.

    Filters here operate on community data (name, labels). For address/geo
    filtering use LocationQuerySet; for per-user filtering use PinQuerySet.
    """

    tree_parent_field = "parent_wiki"

    def child_wikis(self) -> Self:
        """Return only child wikis (community sub-markers nested under a parent wiki)."""
        return self.filter(parent_wiki__isnull=False)


_WikiManagerBase = abstract.PublicDashboardManager.from_queryset(WikiQuerySet)


class WikiManager(_WikiManagerBase["Wiki"]):
    """Manager for Wiki.
    Every pinned Location gets a page automatically (``tasks.ensure_wiki_for_location``), published from the moment it exists and filled in by background enrichment.
    """

    def existing_for_location(self, location: Location | None) -> Wiki | None:
        """The Wiki describing what this Location stands on, draft or official.
        Checks the Location's own row first, then the *place* it resolved onto.
        The second lookup is the dedup that matters: two people pinning opposite ends of one property get two Locations, and without it they would get two community pages for one real-world thing.
        On a campus, whose buildings often have no place for a point to resolve onto, a Location standing on one of them is that building's (``building_wikis.standing_building``).

        Args:
            location: The shared Location to look up (None-safe).

        Returns:
            The Wiki, or None - also for a Location on a campus building that has no wiki yet.
        """
        return self._resolve(location)[0]

    def _resolve(self, location: Location | None) -> tuple[Wiki | None, Wiki | None]:
        """:meth:`existing_for_location`'s answer, with the wiki holding the Location's place when it read one."""
        if location is None:
            return None, None
        try:
            return location.wiki, None
        except ObjectDoesNotExist:
            pass
        holder = self.holding_place_of(location)
        if holder is None:
            return None, None
        from urbanlens.dashboard.services.wiki.building_wikis import standing_building

        building = standing_building(location, holder)
        return (holder if building is None else building.wiki), holder

    def holding_place_of(self, location: Location | None) -> Wiki | None:
        """The wiki holding the place a Location resolved onto, whichever part of the place the Location stands on.

        Args:
            location: The Location (None-safe).

        Returns:
            The Wiki, or None.
        """
        if location is None or location.place_id is None:
            return None
        return self.filter(place_id=location.place_id).select_related("place", "location").first()

    def get_for_location(self, location: Location | None) -> Wiki | None:
        """Return the Location's Wiki, or None when it has none yet.
        Identical to :meth:`existing_for_location`, and kept because it is the name most call sites use.

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
        """Return the Wiki for a Location, creating it if absent, nested where the places already say it belongs.
        The one creation path.
        Everything else should use ``get_for_location``, which never creates - a wiki appearing as a side effect of viewing or editing other content is a bug.

        Args:
            location: The shared Location to attach the wiki to.
            defaults: Optional field overrides for the created Wiki. A ``name``
                key wins over the location's ``provider_name``, which is adopted as a
                stand-in that later public names may replace.

        Returns:
            Tuple of (Wiki, created).
        """
        existing, holder = self._resolve(location)
        if existing is not None:
            return existing, False

        from urbanlens.dashboard.models.pin.model import PinType

        defaults = dict(defaults or {})
        explicit_name = defaults.pop("name", None)
        while True:
            # With no wiki found, a place another wiki holds is a campus's, and this location stands on one of its
            # buildings: the building's wiki holds no place.
            if holder is not None:
                defaults.setdefault("pin_type", PinType.BUILDING)
            try:
                # Both `location` and `place` are one-to-one, so a concurrent create for this Location, or for another
                # Location on the same place, fails here; the savepoint keeps a caller's transaction usable.
                with transaction.atomic():
                    wiki = self._create(location, None if holder is not None else location.place_id, explicit_name, defaults)
                break
            except IntegrityError:
                # Drops the reverse `wiki` cache, which holds either the earlier miss or the instance that failed to insert.
                location.refresh_from_db()
                tried_without_place = holder is not None
                existing, holder = self._resolve(location)
                if existing is not None:
                    return existing, False
                if tried_without_place or holder is None:
                    raise

        from urbanlens.dashboard.services.wiki.wiki_merge import reconcile_wiki_nesting
        from urbanlens.dashboard.services.wiki.wiki_naming import OFFICIAL_NAME_SOURCE, adopt_public_name

        if not explicit_name:
            adopt_public_name(wiki, location.provider_name, source=OFFICIAL_NAME_SOURCE)
        # From the places already stored, so a building's wiki nests without waiting on boundary generation.
        if reconcile_wiki_nesting(wiki):
            wiki.refresh_from_db(fields=["parent_wiki"])
        return wiki, True

    def _create(self, location: Location, place_id: int | None, explicit_name: str | None, defaults: dict) -> Wiki:
        """Insert the wiki row, named as given or, as an automatic write, after the location."""
        from urbanlens.dashboard.models.abstract.versioning import WriteSource, writing_as

        if explicit_name:
            return self.create(location=location, place_id=place_id, name=explicit_name, **defaults)
        # Nobody chose this name, even when a request triggered the creation, so a better public name may replace it.
        with writing_as(WriteSource.AUTOMATIC):
            return self.create(location=location, place_id=place_id, name=self._placeholder_name(location), **defaults)
