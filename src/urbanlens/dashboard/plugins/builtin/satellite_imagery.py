"""Satellite imagery plugins: providers for the pin detail satellite carousel.

Plugin ``order`` values control carousel slide order (Google Maps, defined in
its own module, is 10).

Esri stays a direct integration - not just "current imagery" but up to five
evenly-spaced historical Wayback World Imagery releases, a comparison-over-time
feature core to this app's urbex-documentation purpose (see ``EsriGateway``)
that REData's one-current-result-per-provider ``/imagery/`` contract can't
reproduce. Every other direct satellite-imagery integration this project used
to maintain (NASA GIBS, Mapbox, Bing Maps, OpenAerialMap, OpenTopoMap, and
Azure Maps' imagery half - see ``plugins.builtin.azure_maps``) was a single
current-image-per-provider fit for that contract and has been removed in
favor of :class:`RedataSatelliteProvider`, below - one REData call standing in
for five separate vendor integrations and credentials.

USGS Historical Topo Maps is a different kind of feature entirely (a gallery
of individually dated/titled scanned quadrangles, not one carousel slide) and
was never part of this carousel - see ``plugins.builtin.usgs``, untouched by
this migration.
"""

from __future__ import annotations

import base64
import logging
import math
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.apis.locations.base import SatelliteSlide, SatelliteViewProvider
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError, redata_configured
from urbanlens.dashboard.services.apis.locations.redata_imagery_gateway import RedataImageryGateway
from urbanlens.dashboard.services.core.rate_limiter import ServiceDefaults

if TYPE_CHECKING:
    from collections.abc import Generator

logger = logging.getLogger(__name__)

#: REData imagery providers requested here, and their carousel display name.
#: Deliberately excludes ``esri_world_imagery``/``esri_wayback``/``usgs_imagery``
#: (covered more richly by the direct ``EsriGateway`` above) and ``usgs_topo``
#: (belongs to the separate USGS Historical Topo Maps panel, not this carousel).
_REDATA_PROVIDER_NAMES: dict[str, str] = {
    "open_aerial_map": "OpenAerialMap",
    "nasa_gibs": "NASA GIBS",
    "opentopomap": "OpenTopoMap",
    "mapbox": "Mapbox",
    "bing_maps": "Bing Maps",
    "azure_maps": "Azure Maps",
}

#: These three need a vendor credential REData holds - their ``url`` is
#: REData's own authenticated download proxy, not a publicly fetchable image
#: (see ``RedataImageryGateway.download_bytes``), so their bytes are fetched
#: server-side and embedded as a ``data:`` URI, same as this project's own
#: (now-retired) direct Mapbox/Bing/Azure gateways used to do.
_KEYED_PROVIDERS = frozenset({"mapbox", "bing_maps", "azure_maps"})

#: Representative zoom for a ``tile_template`` delivery (only ``opentopomap`
#: uses one today) - matches the retired direct ``OpenTopoMapGateway``'s own
#: default; trail/terrain context doesn't need a sharper zoom than this.
_TILE_TEMPLATE_ZOOM = 15


def _lonlat_to_tile(longitude: float, latitude: float, zoom: int) -> tuple[int, int]:
    """Web Mercator lon/lat -> slippy-map tile (x, y) at a given zoom."""
    latitude = max(min(latitude, 85.05112878), -85.05112878)
    lat_rad = math.radians(latitude)
    n = 2**zoom
    x = int((longitude + 180.0) / 360.0 * n)
    y = int((1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def _resolve_tile_template(url: str, latitude: float, longitude: float, attributes: dict[str, Any]) -> str:
    """Substitute a specific tile's ``{z}``/``{x}``/``{y}``/``{s}`` into a slippy-map template.

    Args:
        url: The template URL, e.g. ``"https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png"``.
        latitude: WGS-84 latitude to resolve a tile for.
        longitude: WGS-84 longitude to resolve a tile for.
        attributes: The imagery result's ``attributes`` blob, for ``subdomains``.

    Returns:
        A concrete, directly-fetchable tile URL.
    """
    x, y = _lonlat_to_tile(longitude, latitude, _TILE_TEMPLATE_ZOOM)
    subdomains = attributes.get("subdomains") or ["a"]
    return url.format(z=_TILE_TEMPLATE_ZOOM, x=x, y=y, s=subdomains[0])


class EsriPlugin(UrbanLensPlugin):
    """Esri World Imagery satellite basemaps."""

    name: ClassVar[str] = "esri"
    verbose_name: ClassVar[str] = "Esri World Imagery"
    description: ClassVar[str] = "Esri ArcGIS World Imagery (including Wayback historical imagery) in the satellite carousel."
    author: ClassVar[str] = "UrbanLens"
    order: ClassVar[int] = 20

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the public Esri REST services."""
        return {
            "esri": ServiceDefaults(
                display_name="Esri ArcGIS REST",
                calls_per_minute=20,
                calls_per_day=500,
                notes="Public Esri basemap/wayback services. No key required.",
            ),
        }

    def get_satellite_providers(self) -> list[SatelliteViewProvider]:
        """Contribute Esri satellite imagery."""
        from urbanlens.dashboard.services.apis.locations.esri import EsriGateway

        return [EsriGateway()]


class RedataSatelliteProvider(SatelliteViewProvider):
    """Satellite carousel slides from every REData imagery provider not already covered by Esri.

    One REData ``/imagery/`` call stands in for what used to be five separate
    direct integrations (NASA GIBS, Mapbox, Bing Maps, OpenAerialMap,
    OpenTopoMap) - see the module docstring for why those were removed
    outright rather than kept as fallbacks.
    """

    service_key: ClassVar[str] = "redata_imagery"
    paid_service: ClassVar[bool] = False

    def _generate_satellite_slides(
        self,
        latitude: float,
        longitude: float,
        *,
        zoom: int = 17,
        width: int = 640,
        height: int = 400,
        limit: int = -1,
    ) -> Generator[SatelliteSlide]:
        if not redata_configured():
            return

        gateway = RedataImageryGateway()
        try:
            results = gateway.get_imagery(latitude, longitude, providers=list(_REDATA_PROVIDER_NAMES))
        except LocationContextUnavailableError as exc:
            logger.debug("REData imagery unavailable for %s, %s: %s", latitude, longitude, exc)
            return

        seen_urls: set[str] = set()
        for result in results:
            slide = self._slide_from_result(gateway, result, latitude, longitude)
            if slide is not None:
                seen_urls.add(slide.img_src)
                yield slide

        # Dated historical captures. `/imagery/` answers "what can I show for
        # this point now"; the timeline answers "what dates exist", and for a
        # site that has been demolished, re-roofed or cleared, the older frames
        # are the interesting ones. Each capture carries a full `/imagery/` row
        # as its asset, so the same slide builder handles them.
        yield from self._historical_slides(gateway, latitude, longitude, seen_urls)

    def _historical_slides(
        self,
        gateway: RedataImageryGateway,
        latitude: float,
        longitude: float,
        seen_urls: set[str],
    ) -> Generator[SatelliteSlide]:
        """Slides for dated captures from the imagery timeline.

        Continuous ``time_series`` ranges are deliberately skipped: they are a
        date *range* to be materialised one date at a time
        (``POST /imagery/capture/``), not images that already exist, so putting
        them in a carousel would mean inventing dates to show.

        Args:
            gateway: The imagery gateway to reuse.
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            seen_urls: Slide sources already yielded, so a capture that is also
                the current image is not shown twice.

        Yields:
            One slide per dated capture, newest first.
        """
        from urbanlens.dashboard.services.locations.imagery_timeline import flatten_timeline

        try:
            envelope = gateway.get_timeline(latitude, longitude)
        except LocationContextUnavailableError as exc:
            logger.debug("REData imagery timeline unavailable for %s, %s: %s", latitude, longitude, exc)
            return

        for entry in flatten_timeline(envelope):
            if entry["kind"] != "capture":
                continue
            asset = entry.get("asset") or {}
            if not asset.get("url"):
                continue
            slide = self._slide_from_result(gateway, asset, latitude, longitude)
            if slide is None or slide.img_src in seen_urls:
                continue
            seen_urls.add(slide.img_src)
            # An unresolved Esri date is the publication date, months off the
            # acquisition - say so rather than captioning it as fact.
            date = entry["captured_on"] if entry["date_is_exact"] else f"{entry['captured_on']} (published)"
            yield SatelliteSlide(img_src=slide.img_src, source=slide.source, date=str(date), detail=slide.detail)

    def _slide_from_result(self, gateway: RedataImageryGateway, result: dict[str, Any], latitude: float, longitude: float) -> SatelliteSlide | None:
        """Build one carousel slide from a REData imagery result, or None to skip it."""
        provider = result.get("provider")
        if not isinstance(provider, str):
            return None
        name = _REDATA_PROVIDER_NAMES.get(provider)
        url = result.get("url")
        if name is None or not url:
            return None

        if result.get("delivery") == "tile_template":
            img_src = _resolve_tile_template(url, latitude, longitude, result.get("attributes") or {})
        elif provider in _KEYED_PROVIDERS:
            try:
                image_bytes = gateway.download_bytes(url)
            except LocationContextUnavailableError as exc:
                logger.debug("REData imagery download failed for provider %s: %s", provider, exc)
                return None
            img_src = f"data:image/jpeg;base64,{base64.b64encode(image_bytes).decode('ascii')}"
        else:
            img_src = url

        date = result.get("captured_label") or result.get("captured_on") or "Current"
        return SatelliteSlide(img_src=img_src, source=name, date=str(date), detail=result.get("attribution") or "")


class RedataImageryPlugin(UrbanLensPlugin):
    """REData-backed satellite imagery: NASA GIBS, Mapbox, Bing Maps, OpenAerialMap, OpenTopoMap."""

    name: ClassVar[str] = "redata_imagery"
    verbose_name: ClassVar[str] = "REData Imagery"
    description: ClassVar[str] = "Additional satellite/aerial/topographic imagery providers (NASA GIBS, Mapbox, Bing Maps, OpenAerialMap, OpenTopoMap) in the pin detail satellite carousel, via REData. Requires REData to be configured."
    author: ClassVar[str] = "UrbanLens"
    order: ClassVar[int] = 70

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for REData's imagery endpoint."""
        return {
            "redata_imagery": ServiceDefaults(
                display_name="REData Imagery",
                # Shares REData's single 1,000 req/hour "lookup" pool with
                # geocode/weather/routing/etc. - see rate_limiter.SERVICE_REGISTRY's
                # redata_geocode entry for why this side stays conservative anyway.
                calls_per_minute=20,
                calls_per_day=None,
                notes="Aerial/satellite/topographic imagery via GET /imagery/ - every non-Esri provider in one call. See services.apis.locations.redata_imagery_gateway.",
            ),
        }

    def get_satellite_providers(self) -> list[SatelliteViewProvider]:
        """Contribute the REData-backed satellite imagery provider."""
        return [RedataSatelliteProvider()]
