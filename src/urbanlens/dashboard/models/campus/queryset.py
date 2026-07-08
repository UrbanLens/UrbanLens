"""Campus queryset and manager."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Self

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.wiki.model import Wiki

logger = logging.getLogger(__name__)


class CampusQuerySet(abstract.DashboardQuerySet):
    """QuerySet for Campus - spatial region data for a Wiki or Pin."""

    def defaults(self) -> Self:
        """Wiki-level default campuses (profile=None, pin=None)."""
        return self.filter(profile__isnull=True, pin__isnull=True)

    def for_profile(self, profile) -> Self:
        """Pin-scoped campuses belonging to a given profile."""
        return self.filter(profile=profile, pin__isnull=False)

    def for_wiki(self, wiki) -> Self:
        """All campuses (default and pin-scoped) tied to a given wiki."""
        return self.filter(wiki=wiki)

    def for_location(self, location) -> Self:
        """Default campuses whose wiki currently points at the given location."""
        return self.filter(wiki__location=location)

    def for_pin(self, pin) -> Self:
        """Pin-scoped campus for a specific pin."""
        return self.filter(pin=pin)

    def with_coordinate_location(self) -> Self:
        """Prefetch wiki/location/pin so effective_polygon avoids extra queries."""
        return self.select_related("wiki__location", "location", "pin__location")


class CampusManager(abstract.DashboardManager.from_queryset(CampusQuerySet)):
    """Manager for Campus.

    Use effective_for_wiki(wiki) for wiki page lookups and effective_for_pin(pin)
    for pin detail lookups.
    """

    def effective_for_wiki(self, wiki, profile=None):
        """Return the wiki-default Campus for a given wiki.

        Args:
            wiki: Wiki instance or pk.
            profile: Ignored; kept for backwards compatibility.

        Returns:
            Campus | None
        """
        return (
            self.filter(wiki=wiki, profile__isnull=True, pin__isnull=True)
            .with_coordinate_location()
            .first()
        )

    def effective_for(self, location, profile=None):
        """Return the wiki-default Campus for a location's community page.

        Deprecated alias for code still passing a Location; resolves through the
        linked Wiki when present.
        """
        from django.core.exceptions import ObjectDoesNotExist

        from urbanlens.dashboard.models.wiki.model import Wiki

        try:
            wiki = location.wiki
        except ObjectDoesNotExist:
            wiki, _created = Wiki.objects.get_or_create_for_location(location)
        return self.effective_for_wiki(wiki, profile=profile)

    def get_or_create_default_for_wiki(self, wiki : Wiki, *, location: Location | None = None, defaults: dict[str, Any] | None = None):
        """Get or create the wiki-default campus row for a community page."""
        lookup = {"wiki": wiki, "profile": None, "pin": None}
        create_defaults = dict(defaults or {})
        if location is not None:
            create_defaults.setdefault("location", location)
        elif wiki.location_id:
            create_defaults.setdefault("location", wiki.location)
        return self.get_or_create(**lookup, defaults=create_defaults)

    def effective_for_pin(self, pin):
        """Return the Campus to display for a given pin."""
        if pin_campus := self.filter(pin=pin).with_coordinate_location().first():
            return pin_campus
        if pin.wiki_id:
            return self.effective_for_wiki(pin.wiki)
        if pin.location_id:
            return self.filter(wiki__location_id=pin.location_id, profile__isnull=True, pin__isnull=True).with_coordinate_location().first()
        return None
