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

    def renderable(self) -> Self:
        """Overlays that still have an image to draw.

        An overlay whose uploaded ``Image`` was deleted elsewhere (gallery
        cleanup, a quota sweep) keeps its georeferencing but has nothing to
        show; excluding it here stops every map from rendering a broken tile
        for it.
        """
        from django.db.models import Q

        return self.exclude(Q(image__isnull=True) & Q(image_url=""))


class MapImageOverlayManager(abstract.FrontendDashboardManager.from_queryset(MapImageOverlayQuerySet)):
    """Manager for MapImageOverlay."""
