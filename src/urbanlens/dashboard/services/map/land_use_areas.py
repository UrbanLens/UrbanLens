"""The Private Pin map's Land Use layer: Census Special Land Use Area boundaries around a pin's parcel, via REData.

REData answers which of the four Special Land Use Areas (military installation, correctional facility, national park,
college or university) a parcel's coordinate falls inside on every parcel record, and draws their boundaries only on
request. The layer reads the record the Property Records card already fetched, asks for boundaries only when the record
says the parcel is inside one, and keeps the answer per parcel.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.core.cache import DEFAULT_CACHE_ALIAS

from urbanlens.dashboard.models.cache.location_cache import UNANSWERED_SOURCES_KEY, LocationCache
from urbanlens.dashboard.plugins.builtin.property_records import SPECIAL_LAND_USE_LABELS, PropertyRecordsPanelSource
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import redata_configured
from urbanlens.dashboard.services.apis.property_records.redata_gateway import PropertyRecordsBusyError, PropertyRecordsUnavailableError, RedataGateway
from urbanlens.dashboard.services.apis.request_upstreams import LandUseAreasUpstream
from urbanlens.dashboard.services.core.bounded_cache import get_or_none, set_or_skip
from urbanlens.dashboard.services.core.gateway import UPSTREAM_BUSY_MAX_SECONDS
from urbanlens.dashboard.services.geo.geo_boundary import USA

if TYPE_CHECKING:
    from collections.abc import Callable

    from urbanlens.dashboard.models.pin.model import Pin

#: The layer's ``data-map-layer`` key, registered in ``templatetags.map_components``.
LAND_USE_LAYER_KEY = "landuse"

#: How long an answer that drew every area the parcel record named is kept. Special Land Use Area boundaries change
#: with the Census Bureau's yearly TIGER release.
COMPLETE_ANSWER_SECONDS = 7 * 24 * 60 * 60
#: An empty answer for a parcel whose record names no areas to expect, from a REData too old to say: nothing there, or
#: nothing drawn, cannot be told apart.
UNCONFIRMED_ANSWER_SECONDS = 60 * 60
#: An answer missing an area the parcel record named, which REData answers when a live boundary query fails.
PARTIAL_ANSWER_SECONDS = 5 * 60
#: The shortest a failure is remembered, so each toggle during an outage does not ask again.
_FAILURE_SECONDS = 5 * 60

_ATTRIBUTION = "U.S. Census Bureau TIGERweb, via REData"
_BUSY_MESSAGE = "Land-use boundaries are temporarily unavailable. Please try again shortly."
_DRAWABLE_TYPES = frozenset({"Polygon", "MultiPolygon"})
_LABELS = dict(SPECIAL_LAND_USE_LABELS)
_ORDER = {category: index for index, (category, _label) in enumerate(SPECIAL_LAND_USE_LABELS)}

STATUS_FOUND = "found"
STATUS_NONE = "none"
STATUS_NO_PARCEL = "no_parcel"
STATUS_UNAVAILABLE = "unavailable"
STATUS_OUTSIDE_COVERAGE = "outside_coverage"


class LandUseAreasBusyError(Exception):
    """REData could not answer for now; ask again after ``retry_after`` seconds.

    Attributes:
        retry_after: Seconds until asking again is worthwhile.
    """

    def __init__(self, message: str, *, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def land_use_layer_offered(pin: Pin) -> bool:
    """Whether the pin's map offers the Land Use layer: REData is configured and the pin is in the United States.

    Args:
        pin: The pin whose map is being drawn.

    Returns:
        True when the layer could have something to draw.
    """
    return redata_configured() and _in_coverage(pin)


def land_use_area_collection(pin: Pin, *, caller: str | None = None) -> dict[str, Any]:
    """The Special Land Use Areas around a pin's parcel, as a GeoJSON FeatureCollection.

    REData is asked under :class:`LandUseAreasUpstream`'s deadline, slots and per-caller rate.

    Args:
        pin: The pin whose parcel is asked about.
        caller: Who to charge for REData calls (``account_or_address``), or None.

    Returns:
        A FeatureCollection with ``status`` (``found``, ``none``, ``no_parcel``, ``unavailable`` or ``outside_coverage``),
        ``complete`` (False when REData drew fewer areas than the parcel record named) and ``attribution``. Each feature's
        properties are ``category``, ``label`` and ``name``.

    Raises:
        LandUseAreasBusyError: REData, or a source behind it, could not answer for now.
    """
    if not redata_configured():
        return _collection(STATUS_UNAVAILABLE)
    if not _in_coverage(pin):
        return _collection(STATUS_OUTSIDE_COVERAGE)

    record = _parcel_record(pin, caller)
    parcel_uuid = str(record.get("uuid") or "") if record.get("available") else ""
    if not parcel_uuid:
        return _collection(STATUS_NO_PARCEL)

    expected = _expected_categories(record)
    if expected == frozenset():
        return _collection(STATUS_NONE)
    return _cached_areas(parcel_uuid, expected, caller)


def _in_coverage(pin: Pin) -> bool:
    location = pin.location
    if location is None or location.latitude is None or location.longitude is None:
        return False
    return USA.contains(float(location.latitude), float(location.longitude))


def _parcel_record(pin: Pin, caller: str | None) -> dict[str, Any]:
    """The parcel record the Property Records card cached for the pin, else REData's answer for its coordinate.

    The parcel's uuid and land-use flags do not go stale the way its owners do, so a cached record of any age is read.
    Without one, :meth:`RedataGateway.lookup_parcel` shares the card's own in-flight or recent lookup.

    Raises:
        LandUseAreasBusyError: REData could not look the parcel up for now.
    """
    cached = LocationCache.objects.filter(location=pin.location, source=PropertyRecordsPanelSource.cache_source, audience="").values_list("data", flat=True).first()
    if isinstance(cached, dict):
        return cached
    latitude, longitude, address = pin.effective_latitude, pin.effective_longitude, pin.location.address or ""
    try:
        # The card's cached payload marks a found record ``available``; the gateway's bare record does not.
        return {**_ask(lambda: RedataGateway().lookup_parcel(latitude, longitude, situs_address=address), caller), "available": True}
    except PropertyRecordsUnavailableError as exc:
        if exc.is_outage:
            raise _busy(exc) from exc
        return {}


def _expected_categories(record: dict[str, Any]) -> frozenset[str] | None:
    """The categories the parcel record says the parcel is inside.

    Returns:
        The categories; empty when the record says none; None when the record cannot say - a REData that sends no
        flags, or a record some of whose sources did not answer.
    """
    flags = record.get("special_land_use_areas")
    if not isinstance(flags, dict):
        return None
    expected = frozenset(str(category) for category, area in flags.items() if area)
    if not expected and record.get(UNANSWERED_SOURCES_KEY):
        return None
    return expected


def _cached_areas(parcel_uuid: str, expected: frozenset[str] | None, caller: str | None) -> dict[str, Any]:
    """Ask REData for the parcel's boundaries, keeping the answer, and a failure briefly.

    Raises:
        LandUseAreasBusyError: REData could not answer for now, or did not within the last failure's wait.
    """
    key = f"map-land-use-areas:v1:{parcel_uuid}"
    failure_key = f"{key}:failure"
    cached = get_or_none(key, label="land-use areas")
    if isinstance(cached, dict):
        return cached
    failure = get_or_none(failure_key, label="land-use areas failure", alias=DEFAULT_CACHE_ALIAS)
    if isinstance(failure, dict):
        raise LandUseAreasBusyError(str(failure.get("message") or ""), retry_after=int(failure.get("retry_after") or _FAILURE_SECONDS))

    try:
        return _ask(lambda: _fetch_and_keep(key, parcel_uuid, expected), caller)
    except PropertyRecordsUnavailableError as exc:
        if exc.status_code == 404:
            # A REData that predates the endpoint, or no longer holds the parcel: nothing to draw, and nothing to retry.
            return _collection(STATUS_UNAVAILABLE)
        busy = _busy(exc)
        set_or_skip(failure_key, {"message": str(busy), "retry_after": busy.retry_after}, max(busy.retry_after, _FAILURE_SECONDS), label="land-use areas failure", alias=DEFAULT_CACHE_ALIAS)
        raise busy from exc


def _fetch_and_keep(key: str, parcel_uuid: str, expected: frozenset[str] | None) -> dict[str, Any]:
    """Fetch the parcel's boundaries and keep them, so an answer that outran the deadline still serves the next ask.

    Raises:
        PropertyRecordsUnavailableError: REData did not answer.
    """
    areas = RedataGateway().lookup_land_use_areas(parcel_uuid)
    features = sorted((feature for category, area in areas.items() if (feature := _feature(category, area))), key=lambda feature: _ORDER.get(feature["properties"]["category"], len(_ORDER)))
    drawn = frozenset(feature["properties"]["category"] for feature in features)
    complete = expected is None or expected <= drawn
    collection = _collection(STATUS_FOUND if features else STATUS_NONE, features=features, complete=complete)
    if not complete:
        lifetime = PARTIAL_ANSWER_SECONDS
    elif expected is None and not features:
        lifetime = UNCONFIRMED_ANSWER_SECONDS
    else:
        lifetime = COMPLETE_ANSWER_SECONDS
    set_or_skip(key, collection, lifetime, label="land-use areas")
    return collection


def _ask[T](fetch: Callable[[], T], caller: str | None) -> T:
    """Run one REData call under :class:`LandUseAreasUpstream`.

    Raises:
        PropertyRecordsUnavailableError: REData answered with a failure, re-raised for the caller to read.
        LandUseAreasBusyError: The call was throttled, found no free slot, or outran the deadline.
    """
    result = LandUseAreasUpstream.call(fetch, caller=caller)
    if result.ok and result.value is not None:
        return result.value
    if isinstance(result.error, PropertyRecordsUnavailableError):
        raise result.error
    raise LandUseAreasBusyError(_BUSY_MESSAGE, retry_after=max(1, result.retry_after or LandUseAreasUpstream.busy_retry_seconds))


def _busy(exc: PropertyRecordsUnavailableError) -> LandUseAreasBusyError:
    named = exc.retry_after if isinstance(exc, PropertyRecordsBusyError) else 0
    return LandUseAreasBusyError(_BUSY_MESSAGE, retry_after=max(1, min(named or _FAILURE_SECONDS, UPSTREAM_BUSY_MAX_SECONDS)))


def _feature(category: str, area: dict[str, Any]) -> dict[str, Any] | None:
    """One area as a GeoJSON Feature, or None when its geometry is not a polygon Leaflet can draw."""
    geometry = area.get("geometry")
    if not isinstance(geometry, dict) or geometry.get("type") not in _DRAWABLE_TYPES or not isinstance(geometry.get("coordinates"), list):
        return None
    label = _LABELS.get(category) or category.replace("_", " ").capitalize()
    name = str(area.get("name") or "").strip()
    return {
        "type": "Feature",
        "geometry": {"type": geometry["type"], "coordinates": geometry["coordinates"]},
        "properties": {"category": category, "label": label, "name": name or label},
    }


def _collection(status: str, *, features: list[dict[str, Any]] | None = None, complete: bool = True) -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": features or [], "status": status, "complete": complete, "attribution": _ATTRIBUTION}
