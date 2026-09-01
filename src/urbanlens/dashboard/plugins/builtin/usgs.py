"""USGS plugin: historical topographic map panel on the Private Pin page."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.core.rate_limiter import ServiceDefaults
from urbanlens.dashboard.services.pins.external_data import LocationCachePanelSource, PanelApiKind, info_card

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.pins.external_data import PanelSource

#: Ceiling on how many scans one payload carries, matching the ``[:20]`` the
#: HTML panel applies. TNM can return well over a hundred products for a
#: heavily-surveyed quadrangle, and every one of them is another thumbnail a
#: client would try to load.
_MAX_MAPS = 20

#: Attribution string on each map's media item. Spelled out rather than reusing
#: the panel ``title``, because a media item is shown detached from its panel
#: (in a merged gallery) where "USGS Historical Topo Maps" alone reads as a
#: section heading rather than as the source of that particular scan.
_MEDIA_SOURCE_LABEL = "USGS Historical Topographic Map Collection"

#: Where the full collection is browsable. Doubles as the panel's footer link
#: and as each item's ``page_url``, since individual HTMC products have no
#: stable human-facing page - only a direct download.
_TOPOVIEW_URL = "https://ngmdb.usgs.gov/topoview/"


class UsgsTopoPanelSource(LocationCachePanelSource):
    """USGS Historical Topographic Map Collection maps near the pin."""

    key = "usgs_topo"
    cache_source = "usgs_topo"
    section_id = "usgs-topo-section"
    icon = "terrain"
    title = "USGS Historical Topo Maps"
    # Genuinely both shapes, which is exactly what the web panel already is: a
    # summary header plus a grid of thumbnails linking to scans. A client that
    # only lays out information cards still gets the count and the TopoView
    # link; one with a gallery gets the scans themselves.
    api_kinds: ClassVar[frozenset[PanelApiKind]] = frozenset({PanelApiKind.INFO, PanelApiKind.MEDIA})

    def fetch(self, pin: Pin) -> None:
        """Query the TNM API for historical topo maps and cache the result."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.locations.usgs import UsgsGateway

        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        result = UsgsGateway().historical_topo_maps_for_coordinates(lat, lng, delta=0.01)
        LocationCache.set(pin.location, self.cache_source, result or {}, query_key=f"{lat:.4f},{lng:.4f}")

    def api_payload(self, pin: Pin) -> dict[str, Any] | None:
        """The nearby historical topo scans, as both a summary card and media items.

        Field names (``downloadURL``, ``previewGraphicURL``,
        ``publicationDate``, ``extent``) are TNM's own camelCase and are read
        here exactly as ``pin_usgs_topo.html`` reads them - they are not
        renamed on the way out of the cache, so the two surfaces can't drift
        into disagreeing about which key holds what.

        Args:
            pin: The pin whose panel is being read.

        Returns:
            ``{"info": {...}, "media": [...]}``, or None when nothing has
            landed yet or the search area contains no scanned maps (the same
            condition the web panel 204s on).
        """
        data = self.cached_data(pin)
        if data is None:
            return None
        maps = (data.get("items") or [])[:_MAX_MAPS]
        if not maps:
            return None

        media = [
            {
                "url": topo_map.get("downloadURL") or "",
                "thumb_url": topo_map.get("previewGraphicURL") or "",
                "caption": self._caption(topo_map),
                "source": _MEDIA_SOURCE_LABEL,
                "page_url": _TOPOVIEW_URL,
            }
            for topo_map in maps
            if topo_map.get("downloadURL")
        ]

        return {
            PanelApiKind.INFO.value: info_card(
                chips=[f"{len(maps)} maps"],
                footer_link={"url": _TOPOVIEW_URL, "label": "View all on USGS TopoView"},
            ),
            PanelApiKind.MEDIA.value: media,
        }

    @staticmethod
    def _caption(topo_map: dict[str, Any]) -> str:
        """Title plus survey year for one scanned map.

        The year is what actually distinguishes two otherwise identically-named
        editions of the same quadrangle, so it is folded into the caption
        rather than left as a separate field the media contract has no room
        for. ``publicationDate`` is an ISO date string; only its year is
        meaningful at this granularity.

        Args:
            topo_map: One TNM HTMC product record.

        Returns:
            A caption like ``"Poughkeepsie, NY (1893)"``, possibly without the
            parenthetical when TNM reported no publication date.
        """
        title = topo_map.get("title") or "Untitled map"
        year = (topo_map.get("publicationDate") or "")[:4]
        return f"{title} ({year})" if year else title


class UsgsPlugin(UrbanLensPlugin):
    """USGS historical topographic maps for pinned locations."""

    name: ClassVar[str] = "usgs"
    verbose_name: ClassVar[str] = "USGS Historical Topo Maps"
    description: ClassVar[str] = "Shows USGS Historical Topographic Map Collection maps near a pin on the Private Pin page. USA only."
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the USGS APIs."""
        return {
            "usgs": ServiceDefaults(
                display_name="USGS EarthExplorer / TNM",
                calls_per_minute=10,
                calls_per_day=500,
                usa_only=True,
                notes="M2M requires an applicationToken from EarthExplorer account settings. TNM is fully public.",
            ),
        }

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the USGS topo-map pin-detail panel."""
        return [UsgsTopoPanelSource()]
