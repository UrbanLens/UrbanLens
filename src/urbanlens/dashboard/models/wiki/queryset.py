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


class WikiManager(abstract.PublicDashboardManager.from_queryset(WikiQuerySet)):
    """Manager for Wiki.
    Every pinned Location gets a page automatically (``tasks.ensure_wiki_for_location``), published from the moment it exists and filled in by background enrichment.
    """

    def existing_for_location(self, location: Location | None) -> Wiki | None:
        """The Wiki describing what this Location stands on, draft or official.
        Checks the Location's own row first, then the *place* it resolved onto.
        The second lookup is the dedup that matters: two people pinning opposite ends of one property get two Locations, and without it they would get two community pages for one real-world thing.

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
        """Return the Wiki for a Location, creating it if absent.
        The one creation path.
        Everything else should use ``get_for_location``, which never creates - a wiki appearing as a side effect of viewing or editing other content is a bug.

        Args:
            location: The shared Location to attach the wiki to.
            defaults: Optional field overrides for the created Wiki. A ``name``
                key wins over the location's ``official_name``, which is adopted as a
                stand-in that later public names may replace.

        Returns:
            Tuple of (Wiki, created).
        """
        if (existing := self.existing_for_location(location)) is not None:
            return existing, False

        from urbanlens.dashboard.models.abstract.versioning import WriteSource, writing_as

        defaults = dict(defaults or {})
        explicit_name = defaults.pop("name", None)
        try:
            # Both `location` and `place` are one-to-one, so a concurrent create for this Location, or for another
            # Location on the same place, fails here; the savepoint keeps a caller's transaction usable.
            with transaction.atomic():
                if explicit_name:
                    return self.create(location=location, place_id=location.place_id, name=explicit_name, **defaults), True
                # Nobody chose this name, even when a request triggered the creation, so a better public name may replace it.
                with writing_as(WriteSource.AUTOMATIC):
                    wiki = self.create(location=location, place_id=location.place_id, name=self._placeholder_name(location), **defaults)
        except IntegrityError:
            # Drops the reverse `wiki` cache, which holds either the earlier miss or the instance that failed to insert.
            location.refresh_from_db()
            if (existing := self.existing_for_location(location)) is None:
                raise
            return existing, False

        from urbanlens.dashboard.services.wiki.wiki_naming import OFFICIAL_NAME_SOURCE, adopt_public_name

        adopt_public_name(wiki, location.official_name, source=OFFICIAL_NAME_SOURCE)
        return wiki, True
