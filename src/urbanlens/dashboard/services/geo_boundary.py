"""Geographic boundary gating for plugins.

Generalizes the old "USA only" plugin flag (previously three inconsistent,
partially-enforced mechanisms - see ``services/geo_filter.py``,
``services/apis/assets/base.py``'s ``MediaProvider.usa_only``, and
``services/enrichment.py``'s ``EnrichmentSource.usa_only``) into a single
:class:`GeoBoundary` value type that can express "USA", a single US state, or
(via :meth:`GeoBoundary.from_bboxes`) any other bounding-box union - not just a
country.

A :class:`GeoBoundary` is safe to build at plugin-class-body/import time (e.g.
as a ``ClassVar``): resolving to real geometry is deferred to first use via a
loader callable, honoring the plugin system's "no database/network in
``__init__``/import" rule (see ``plugins/base.py``'s module docstring).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING

from django.contrib.gis.geos import MultiPolygon, Point, Polygon

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

logger = logging.getLogger(__name__)

#: (lat_min, lat_max, lng_min, lng_max), matching the shape callers already use.
BBox = tuple[float, float, float, float]

#: How long a fetched state polygon stays cached (Django cache, e.g. Redis/Valkey).
#: State boundaries are effectively static, so this is long - a process restart
#: reads from here instead of re-querying TIGERweb.
_STATE_BOUNDARY_CACHE_SECONDS = 30 * 86400


@dataclass(slots=True)
class GeoBoundary:
    """A geographic region, lazily resolved to a GEOS polygon and memoized.

    Attributes:
        _loader: Zero-argument callable returning the boundary's geometry (or
            None when it couldn't be resolved). Invoked at most once per
            instance, on first real use (:meth:`contains` or :attr:`geometry`)
            - never at construction, so assigning a ``GeoBoundary`` as a
            ``ClassVar`` never touches the network or database.
    """

    _loader: Callable[[], Polygon | MultiPolygon | None]
    _cached: Polygon | MultiPolygon | None = field(default=None, init=False, repr=False)
    _loaded: bool = field(default=False, init=False, repr=False)

    def _geometry(self) -> Polygon | MultiPolygon | None:
        if not self._loaded:
            try:
                self._cached = self._loader()
            except Exception:
                # TODO: Catch specific exceptions
                logger.exception("GeoBoundary: loader failed; treating boundary as unavailable")
                self._cached = None
            self._loaded = True
        return self._cached

    @property
    def geometry(self) -> Polygon | MultiPolygon | None:
        """The resolved geometry, for callers that need a raw GEOS value (e.g. a DB spatial filter)."""
        return self._geometry()

    def contains(self, lat: float | None, lng: float | None) -> bool:
        """Return True if (lat, lng) falls within this boundary.

        Accepts ``None`` inputs and returns False (no coordinates -> cannot
        confirm the location is in-boundary -> gate closed), matching the
        behavior of the ``is_usa_coordinates`` helper this generalizes.

        Args:
            lat: WGS-84 latitude.
            lng: WGS-84 longitude.

        Returns:
            True when inside (or touching the edge of) the boundary.
        """
        if lat is None or lng is None:
            return False
        geometry = self._geometry()
        if geometry is None:
            return False
        try:
            point = Point(float(lng), float(lat), srid=4326)
        except (TypeError, ValueError):
            return False
        return geometry.contains(point) or geometry.touches(point)

    @classmethod
    def from_bboxes(cls, boxes: Sequence[BBox]) -> GeoBoundary:
        """Build a boundary from a union of lat/lng bounding boxes (pure math, no I/O).

        Args:
            boxes: Each box as (lat_min, lat_max, lng_min, lng_max).

        Returns:
            A ``GeoBoundary`` covering the union of all boxes.
        """

        def _load() -> MultiPolygon:
            polygons = []
            for lat_min, lat_max, lng_min, lng_max in boxes:
                polygon = Polygon.from_bbox((lng_min, lat_min, lng_max, lat_max))
                polygon.srid = 4326
                polygons.append(polygon)
            return MultiPolygon(polygons, srid=4326)

        return cls(_load)

    @classmethod
    def from_wkt(cls, wkt: str) -> GeoBoundary:
        """Build a boundary from a hand-authored WKT polygon/multipolygon (pure parsing, no I/O).

        The arbitrary-shape counterpart to :meth:`from_bboxes`, for a boundary
        that isn't a rectangle union (e.g. a hand-traced district).

        Args:
            wkt: Well-known text, e.g. ``"POLYGON((...))"``.

        Returns:
            A ``GeoBoundary`` wrapping the parsed geometry.
        """

        def _load() -> Polygon | MultiPolygon:
            from django.contrib.gis.geos import GEOSGeometry

            geometry = GEOSGeometry(wkt)
            if not isinstance(geometry, (Polygon, MultiPolygon)):
                raise TypeError(f"Expected a POLYGON or MULTIPOLYGON WKT, got {geometry.geom_type}")
            geometry.srid = 4326
            return geometry

        return cls(_load)


# Approximate bounding boxes for US territories - moved here verbatim from
# ``geo_filter.py``, which now wraps this boundary instead of maintaining its
# own copy. Intentionally generous - false *negatives* (blocking a US
# location) are worse than false *positives* (allowing a near-miss).
_USA_BBOXES: tuple[BBox, ...] = (
    # Continental United States (conterminous)
    (24.396308, 49.384358, -125.000000, -66.934570),
    # Alaska (main landmass + western islands)
    (54.800000, 71.538800, -168.000000, -130.000000),
    # Aleutian Islands (cross the anti-meridian; split into two boxes)
    (51.200000, 54.800000, -180.000000, -130.000000),
    (51.200000, 54.800000, 171.000000, 180.000000),
    # Hawaii
    (18.910361, 22.235097, -160.300000, -154.806000),
    # Puerto Rico
    (17.831509, 18.516766, -67.942848, -65.221909),
    # U.S. Virgin Islands
    (17.678268, 18.412655, -65.154389, -64.512674),
    # Guam
    (13.182397, 13.706179, 144.573975, 144.954937),
    # American Samoa
    (-14.731771, -14.159447, -170.846497, -169.416504),
    # Northern Mariana Islands
    (14.036565, 20.616555, 144.813338, 146.154418),
)

#: The United States (all territories), as a boundary - the canonical
#: replacement for every plugin that used to set ``usa_only = True``.
USA: GeoBoundary = GeoBoundary.from_bboxes(_USA_BBOXES)


def state_boundary(state_abbr: str) -> GeoBoundary:
    """Return a lazily-loaded boundary for one US state, from Census TIGERweb.

    The fetched polygon is cached in Django's shared cache (state boundaries
    are effectively static) so a process restart doesn't re-fetch, and
    memoized on the returned ``GeoBoundary`` so repeated ``.contains()`` calls
    within one process never repeat even a cache lookup.

    Args:
        state_abbr: Two-letter USPS state abbreviation (e.g. ``"NY"``).

    Returns:
        A ``GeoBoundary`` for the state. Resolves to no geometry (so
        ``.contains()`` always returns False) if TIGERweb has no matching
        state or the request fails.
    """

    def _load() -> Polygon | MultiPolygon | None:
        from django.core.cache import cache

        from urbanlens.dashboard.services.apis.locations.base import esri_rings_to_polygon
        from urbanlens.dashboard.services.apis.locations.census_tigerweb import CensusTigerwebGateway

        cache_key = f"geo_boundary:state:{state_abbr.upper()}"
        rings_payload = cache.get(cache_key)
        if rings_payload is None:
            geometry = CensusTigerwebGateway().get_state_boundary(state_abbr)
            if geometry is None:
                return None
            rings_payload = {"format": "esri_rings", "rings": geometry.get("rings")}
            cache.set(cache_key, rings_payload, _STATE_BOUNDARY_CACHE_SECONDS)
        return esri_rings_to_polygon(rings_payload)

    return GeoBoundary(_load)
