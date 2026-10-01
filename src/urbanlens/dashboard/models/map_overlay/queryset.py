"""QuerySet and manager for georeferenced map image overlays."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.map_overlay.model import MapImageOverlay  # noqa: F401 - mypy needs these; ruff does not


class MapImageOverlayQuerySet(abstract.FrontendDashboardQuerySet["MapImageOverlay"]):
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


_MapImageOverlayManagerBase = abstract.FrontendDashboardManager.from_queryset(MapImageOverlayQuerySet)


class MapImageOverlayManager(_MapImageOverlayManagerBase):
    """Manager for MapImageOverlay."""
