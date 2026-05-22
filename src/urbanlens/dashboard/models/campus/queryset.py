"""Campus queryset and manager."""

from __future__ import annotations

import logging

from urbanlens.dashboard.models import abstract

logger = logging.getLogger(__name__)


class CampusQuerySet(abstract.QuerySet):
    """QuerySet for Campus - spatial region data for a Location.

    Campus is distinct from Location (canonical place data) and Pin (user visit
    records).  Filters here operate on region/boundary data.
    """

    def defaults(self) -> CampusQuerySet:
        """Admin-defined default campuses (profile=None)."""
        return self.filter(profile__isnull=True)

    def for_profile(self, profile) -> CampusQuerySet:
        """User-specific campus overrides belonging to a given profile."""
        return self.filter(profile=profile)

    def for_location(self, location) -> CampusQuerySet:
        """All campuses (default and user-specific) for a given location."""
        return self.filter(location=location)

    def with_location(self) -> CampusQuerySet:
        """Prefetch location so effective_polygon doesn't trigger extra queries."""
        return self.select_related("location")


class CampusManager(abstract.Manager.from_queryset(CampusQuerySet)):
    """Manager for Campus. Use effective_for() to resolve the admin-default / user-override chain."""

    def effective_for(self, location, profile=None):
        """Return the Campus to display for a given location and optional profile.

        Resolution order:
        1. User's personal override, if profile is given and one exists.
        2. Admin-defined default for the location, if one exists.
        3. None - caller should fall back to a generated circle around
           Location.latitude / Location.longitude.

        Args:
            location: Location instance or pk.
            profile: Profile instance, pk, or None for anonymous / no override.

        Returns:
            Campus | None
        """
        qs = self.filter(location=location).select_related("location")

        if profile is not None:
            user_campus = qs.filter(profile=profile).first()
            if user_campus:
                return user_campus

        return qs.filter(profile__isnull=True).first()
