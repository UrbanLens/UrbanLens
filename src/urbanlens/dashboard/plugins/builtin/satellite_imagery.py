"""Satellite imagery plugins: providers for the pin detail satellite carousel.
USGS Historical Topo Maps is a different kind of feature entirely (a gallery of individually dated/titled scanned quadrangles, not one carousel slide) and was never part of this carousel - see ``plugins.builtin.usgs``, untouched by this migration."""

from __future__ import annotations

import base64
import datetime
import logging
import math
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.apis.locations.base import SatelliteSlide, SatelliteViewProvider
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError, redata_configured
from urbanlens.dashboard.services.apis.locations.redata_imagery_gateway import RedataImageryGateway
from urbanlens.dashboard.services.core.rate_limiter import ServiceDefaults
from urbanlens.dashboard.services.security.redact import redact_coordinate

if TYPE_CHECKING:
    from collections.abc import Generator

logger = logging.getLogger(__name__)

#: Carousel display names for REData imagery providers. **Not** the list of providers requested -
#: that comes from REData's own capability index (see :func:`_wanted_providers`), so a source REData
#: registers appears here without an UrbanLens release rather than being silently dropped.
_REDATA_PROVIDER_NAMES: dict[str, str] = {
    "open_aerial_map": "OpenAerialMap",
    "nasa_gibs": "NASA GIBS",
    "opentopomap": "OpenTopoMap",
    "mapbox": "Mapbox",
    "bing_maps": "Bing Maps",
    "azure_maps": "Azure Maps",
    # Global annual cloud-free Sentinel-2 mosaics, one frame per year since 2016.
    # The timeline was already fetching these and the carousel dropped them.
    "s2cloudless": "Sentinel-2 cloudless",
}

#: Providers REData offers that this carousel does not request, each because some *other* surface of
#: this app shows it better.
#: A fact about UrbanLens's own UI, which is what makes it safe to write down - unlike the provider
#: list itself, which is REData's and is discovered.
_SHOWN_ELSEWHERE: frozenset[str] = frozenset(
    {
        "esri_world_imagery",  # EsriPlugin's direct gateway, with more detail
        "esri_wayback",  # likewise - its own dated series
        "usgs_imagery",  # likewise
        "usgs_topo",  # the USGS Historical Topo Maps panel
        "map_warper",  # the historical-map picker: scanned maps, not imagery
    },
)

#: Scanned historical map collections, which arrive as one provider tag per loc.gov collection and
#: are generated on REData's side.
#: Matched by prefix because the set grows there, and none of them belong in a satellite carousel -
#: they are the historical-map picker's material.
_HISTORICAL_MAP_PREFIX = "loc_"


def _wanted_providers(latitude: float, longitude: float) -> list[str]:
    """Which imagery providers to ask for at a point.

    Args:
        latitude: WGS-84 latitude.
        longitude: WGS-84 longitude.

    Returns:
        Provider tags to request, in REData's own order."""
    from urbanlens.dashboard.services.apis.locations.redata_capabilities_gateway import applicable_providers

    discovered = applicable_providers("imagery", latitude, longitude)
    if not discovered:
        return list(_REDATA_PROVIDER_NAMES)
    return [tag for tag in discovered if tag not in _SHOWN_ELSEWHERE and not tag.startswith(_HISTORICAL_MAP_PREFIX)]


_KEYED_PROVIDERS = frozenset({"mapbox", "bing_maps", "azure_maps"})

#: Representative zoom for a ``tile_template`` delivery (only ``opentopomap` uses one today) -
#: matches the retired direct ``OpenTopoMapGateway``'s own default; trail/terrain context doesn't
#: need a sharper zoom than this.
_TILE_TEMPLATE_ZOOM = 15

#: Size for the composed images this module asks REData to render - both a ``tile_template``
#: provider's stitched-from-tiles photo and a materialized ``time_series`` date.
#: Matches the ``/imagery/capture/`` example in REData's docs; big enough to read as a real photo of
#: the place rather than a single tile-sized crop.
_COMPOSED_IMAGE_WIDTH = 1024
_COMPOSED_IMAGE_HEIGHT = 1024


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
        A concrete, directly-fetchable tile URL."""
    x, y = _lonlat_to_tile(longitude, latitude, _TILE_TEMPLATE_ZOOM)
    subdomains = attributes.get("subdomains") or ["a"]
    return url.format(z=_TILE_TEMPLATE_ZOOM, x=x, y=y, s=subdomains[0])


def _most_recent_interval_end(attributes: dict[str, Any]) -> datetime.date | None:
    """The latest date covered by a ``time_series`` result's own ``attributes.intervals``.

    Args:
        attributes: The result's ``attributes`` blob.

    Returns:
        The latest parseable ``end`` across every interval, or None when ``intervals`` is missing or none of it parses as a date."""
    latest: datetime.date | None = None
    for interval in attributes.get("intervals") or []:
        if not isinstance(interval, dict):
            continue
        end = interval.get("end")
        if not isinstance(end, str):
            continue
        try:
            parsed = datetime.date.fromisoformat(end[:10])
        except ValueError:
            continue
        if latest is None or parsed > latest:
            latest = parsed
    return latest


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
    """Satellite carousel slides from every REData imagery provider not already covered by Esri."""

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

        wanted = _wanted_providers(latitude, longitude)
        if not wanted:
            return

        gateway = RedataImageryGateway()
        # Deliberately not swallowed: SatelliteViewProvider.get_satellite_slides distinguishes "this
        # place has no imagery" from "we could not ask", and caches only the first.
        # Catching here would hide the difference and cache an outage as a permanent absence.
        results = gateway.get_imagery(latitude, longitude, providers=wanted)

        seen_urls: set[str] = set()
        for result in results:
            slide = self._slide_from_result(gateway, result, latitude, longitude)
            if slide is not None:
                seen_urls.add(slide.img_src)
                yield slide

        # Dated historical captures.
        # `/imagery/` answers "what can I show for this point now"; the timeline answers "what dates
        # exist", and for a site that has been demolished, re-roofed or cleared, the older frames
        # are the interesting ones.
        yield from self._historical_slides(gateway, latitude, longitude, seen_urls)

    def _historical_slides(
        self,
        gateway: RedataImageryGateway,
        latitude: float,
        longitude: float,
        seen_urls: set[str],
    ) -> Generator[SatelliteSlide]:
        """Slides for dated captures from the imagery timeline.
        ``_slide_from_result`` already materializes one representative date per ``time_series`` provider from the plain ``/imagery/`` call above, so nothing is lost; this loop only ever sees ``"capture"`` offerings.

        Yields:
            One slide per dated capture, newest first."""
        from urbanlens.dashboard.services.locations.imagery_timeline import flatten_timeline

        # Swallowed on purpose, unlike the current-imagery call above: the
        # timeline is an enrichment on top of slides that already exist, so a
        # timeline outage should not discard them or suppress their caching.
        try:
            envelope = gateway.get_timeline(latitude, longitude)
        except LocationContextUnavailableError as exc:
            logger.debug(
                "REData imagery timeline unavailable for %s, %s: %s",
                redact_coordinate(latitude),
                redact_coordinate(longitude),
                exc.reason,
            )
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
        url = result.get("url")
        if not url or provider in _SHOWN_ELSEWHERE or provider.startswith(_HISTORICAL_MAP_PREFIX):
            return None
        # Named if we know it, title-cased from the tag if we do not.
        # Gating on a known name instead is what made a newly-registered REData provider invisible
        # even once it was requested - including through the historical-capture path, which reaches
        # this with rows the carousel never asked for directly.
        name = _REDATA_PROVIDER_NAMES.get(provider) or provider.replace("_", " ").title()

        delivery = result.get("delivery")
        if delivery == "time_series":
            # Not a picture: `url` is a template carrying a literal `{time}` and the row describes a
            # date *range*.
            return self._time_series_slide(gateway, result, name)

        if delivery == "tile_template":
            img_src = self._composed_tile_image(gateway, result, url, latitude, longitude)
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

    def _composed_tile_image(self, gateway: RedataImageryGateway, result: dict[str, Any], url: str, latitude: float, longitude: float) -> str:
        """``img_src`` for a ``tile_template`` result: REData's composed photo, or a raw tile as a fallback.

        Returns:
            A ``data:`` URI with REData's composed image, or a concrete (still directly-fetchable) tile URL when there is no ``uuid`` to ask for or the composed download fails."""
        asset_uuid = result.get("uuid")
        if isinstance(asset_uuid, str) and asset_uuid:
            try:
                image_bytes = gateway.download_archived_copy(asset_uuid, width=_COMPOSED_IMAGE_WIDTH, height=_COMPOSED_IMAGE_HEIGHT)
            except LocationContextUnavailableError as exc:
                logger.debug("REData composed-imagery download failed for asset %s, falling back to a raw tile: %s", asset_uuid, exc)
            else:
                return f"data:image/jpeg;base64,{base64.b64encode(image_bytes).decode('ascii')}"
        else:
            logger.debug("tile_template imagery result for provider %s carries no uuid; falling back to a raw tile.", result.get("provider"))
        return _resolve_tile_template(url, latitude, longitude, result.get("attributes") or {})

    def _time_series_slide(self, gateway: RedataImageryGateway, result: dict[str, Any], name: str) -> SatelliteSlide | None:
        """Materialize and embed one date from a continuous (``time_series``) source.
        This shows exactly one: the range's most recent date (see ``_most_recent_interval_end``), so this provider gets one carousel slide framed the same "current conditions" way every other slide here is, rather than being skipped outright.

        Returns:
            A slide for the materialized date, or None when there is no interval to pick a date from, or REData can't produce an image for it - a documented "nothing here" answer or a transient failure are both treated as an ordinary provider gap here, same..."""
        asset_uuid = result.get("uuid")
        end_date = _most_recent_interval_end(result.get("attributes") or {})
        if not isinstance(asset_uuid, str) or not asset_uuid or end_date is None:
            logger.debug("time_series imagery result for provider %s has no usable uuid/interval; skipping.", result.get("provider"))
            return None

        try:
            captured = gateway.capture_time_series(asset_uuid, end_date, width=_COMPOSED_IMAGE_WIDTH, height=_COMPOSED_IMAGE_HEIGHT)
        except LocationContextUnavailableError as exc:
            logger.debug("REData imagery capture failed for asset %s on %s: %s", asset_uuid, end_date, exc)
            return None
        if captured is None:
            return None

        captured_uuid = captured.get("uuid")
        if not isinstance(captured_uuid, str) or not captured_uuid:
            logger.debug("REData imagery capture for asset %s returned no uuid; skipping.", asset_uuid)
            return None
        try:
            image_bytes = gateway.download_archived_copy(captured_uuid, width=_COMPOSED_IMAGE_WIDTH, height=_COMPOSED_IMAGE_HEIGHT)
        except LocationContextUnavailableError as exc:
            logger.debug("REData imagery download failed for materialized asset %s: %s", captured_uuid, exc)
            return None

        img_src = f"data:image/jpeg;base64,{base64.b64encode(image_bytes).decode('ascii')}"
        return SatelliteSlide(img_src=img_src, source=name, date=end_date.isoformat(), detail=result.get("attribution") or "")


class RedataImageryPlugin(UrbanLensPlugin):
    """REData-backed satellite imagery: Sentinel-2 cloudless, NASA GIBS, Mapbox, Bing Maps, OpenAerialMap, OpenTopoMap."""

    name: ClassVar[str] = "redata_imagery"
    verbose_name: ClassVar[str] = "REData Imagery"
    description: ClassVar[str] = (
        "Additional satellite/aerial/topographic imagery in the pin detail carousel, via REData - including "
        "Sentinel-2 cloudless annual mosaics, which give one frame per year since 2016. Which sources are asked "
        "is discovered from REData's capability index rather than listed here, minus the ones another panel "
        "already shows better. Requires REData to be configured."
    )
    author: ClassVar[str] = "UrbanLens"
    order: ClassVar[int] = 70

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for REData's imagery endpoint."""
        return {
            "redata_imagery": ServiceDefaults(
                display_name="REData Imagery",
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
