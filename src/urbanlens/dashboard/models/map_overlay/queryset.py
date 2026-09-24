"""QuerySet and manager for georeferenced map image overlays."""

from __future__ import annotations

from typing import Self

from urbanlens.dashboard.models import abstract


class MapImageOverlayQuerySet(abstract.FrontendDashboardQuerySet):
    """QuerySet for :class:`~urbanlens.dashboard.models.map_overlay.model.MapImageOverlay`."""

    def for_pin(self, pin) -> Self:
        """Overlays on a specific pin's own detail map."""
        return self.filter(parent_pin=pin)

    def for_wiki(self, wiki) -> Self:
        """Overlays on a specific wiki's shared map."""
        return self.filter(parent_wiki=wiki)

    def for_profile(self, profile) -> Self:
        """Overlays created by a specific profile."""
        return self.filter(profile=profile)


class MapImageOverlayManager(abstract.FrontendDashboardManager.from_queryset(MapImageOverlayQuerySet)):
    """Manager for MapImageOverlay."""
