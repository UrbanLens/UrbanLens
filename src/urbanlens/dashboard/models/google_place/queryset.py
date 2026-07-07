"""QuerySet and manager for GooglePlace."""

from __future__ import annotations

from urbanlens.dashboard.models import abstract


class GooglePlaceQuerySet(abstract.DashboardQuerySet):
    """Query helpers for coordinate-keyed Google Place cache rows."""

    def by_coordinates(self, latitude, longitude):
        """Return rows matching the given WGS-84 coordinates."""
        return self.filter(latitude=latitude, longitude=longitude)

    def by_cid(self, cid: int):
        """Return rows matching a Google Maps CID."""
        return self.filter(cid=cid)


class GooglePlaceManager(abstract.DashboardManager.from_queryset(GooglePlaceQuerySet)):
    """Manager for GooglePlace."""
