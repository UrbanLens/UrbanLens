"""REData's near-point domains, each read once per point and shared by every panel that shows it.

Several panels of one page read the same REData domain, each from its own task as the page opens: the street
carousel's three networks and the Street-level gallery, the Historical Features panel and the time slider, the Aerial
and Nearby Media galleries. Each domain is asked once per point here and the answer shared for a few minutes.

``GET /locations/context/`` is asked first. It answers all of :data:`CONTEXT_DOMAINS` in one call from what REData
already holds, charged to the key's default pool rather than the lookup pool the per-domain endpoints share. A domain
it cannot answer in full - a provider REData has not asked about this point yet - is then asked of its own endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import logging
from typing import TYPE_CHECKING, Any

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError, redata_configured
from urbanlens.dashboard.services.core.coalesce import coalesced

if TYPE_CHECKING:
    from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope

logger = logging.getLogger(__name__)

#: The domains read through the context endpoint, asked together.
CONTEXT_DOMAINS: tuple[str, ...] = ("media", "street_view", "reference_documents", "historical_features")

#: How long one answer is shared. The panels of one page all ask within it; the panels' own caches keep it after.
SHARE_SECONDS = 600


def point_key(latitude: float, longitude: float) -> str:
    """The shared-answer key for a point - the precision the panels' own ``query_key`` records."""
    return f"{latitude:.5f},{longitude:.5f}"


def settled_domain(latitude: float, longitude: float, domain: str) -> LocationContextEnvelope | None:
    """A domain's answer from REData's cache, when every provider covering the point has one.

    Args:
        latitude: WGS-84 latitude.
        longitude: WGS-84 longitude.
        domain: One of :data:`CONTEXT_DOMAINS`.

    Returns:
        The envelope, or None when the domain's own endpoint has to be asked.
    """
    if not redata_configured():
        return None
    answers = coalesced(f"redata:context:{point_key(latitude, longitude)}", lambda: _settled_domains(latitude, longitude), ttl=SHARE_SECONDS)
    return answers.get(domain)


def _settled_domains(latitude: float, longitude: float) -> dict[str, LocationContextEnvelope]:
    """Every domain the context endpoint answers in full; empty when it cannot be read, so each domain asks its own endpoint."""
    from urbanlens.dashboard.services.apis.locations.redata_locations_context_gateway import RedataLocationsContextGateway

    try:
        context = RedataLocationsContextGateway().get_context(latitude, longitude, CONTEXT_DOMAINS)
    except LocationContextUnavailableError as exc:
        logger.info("REData location context unavailable, so each domain asks its own endpoint: %s", exc)
        return {}
    return {domain: envelope for domain in CONTEXT_DOMAINS if (envelope := context.settled(domain)) is not None}


def media_near(latitude: float, longitude: float) -> LocationContextEnvelope:
    """The media items REData has near a point that the Nearby Media and Aerial tabs can show (``MediaItemSerializer`` rows).

    The street-level networks are not asked for: their captures are the Street-level tab's, read by date through
    :func:`street_view_dates`, and a row from them here would be dropped unseen after costing REData a search of the
    network. The context read cannot be narrowed, so a settled answer from it may still hold their rows; the tabs drop
    those.

    Args:
        latitude: WGS-84 latitude.
        longitude: WGS-84 longitude.

    Returns:
        The answer; its rows are a floor rather than a total when it is not ``complete``.

    Raises:
        LocationContextUnavailableError: No provider answered.
    """
    from urbanlens.dashboard.services.apis.locations.redata_media_gateway import NEARBY_MEDIA_PROVIDERS, RedataMediaGateway

    providers = list(NEARBY_MEDIA_PROVIDERS)

    def ask() -> LocationContextEnvelope:
        envelope = settled_domain(latitude, longitude, "media")
        if envelope is not None:
            return envelope
        return RedataMediaGateway().lookup_envelope(latitude, longitude, provider=providers)

    # The provider set is part of the question, so an answer shared for one set is never handed to a reader of another.
    asked = hashlib.sha256(",".join(sorted(providers)).encode()).hexdigest()[:12]
    return coalesced(f"redata:media:{point_key(latitude, longitude)}:{asked}", ask, ttl=SHARE_SECONDS)


@dataclass(frozen=True, slots=True)
class StreetViewDates:
    """The dates a point was photographed from the street, across every network.

    Attributes:
        dates: ``{captured_on, provider, count, is_panoramic, representative}`` per network and date, newest first;
            ``representative`` is a ``StreetViewCaptureSerializer`` row - the frame nearest the point.
        complete: False when a network failed to answer, so the dates are a floor.
    """

    dates: list[dict[str, Any]] = field(default_factory=list)
    complete: bool = True


def street_view_dates(latitude: float, longitude: float) -> StreetViewDates:
    """Street-level capture dates near a point, every network in one answer.

    Args:
        latitude: WGS-84 latitude.
        longitude: WGS-84 longitude.

    Returns:
        The dates.

    Raises:
        LocationContextUnavailableError: No network answered, or the request failed.
    """

    def ask() -> StreetViewDates:
        envelope = settled_domain(latitude, longitude, "street_view")
        if envelope is not None:
            return StreetViewDates(dates=dates_from_captures(envelope.results, latitude, longitude))
        from urbanlens.dashboard.services.apis.locations.redata_street_view_gateway import RedataStreetViewGateway

        timeline = RedataStreetViewGateway().get_timeline(latitude, longitude)
        dates = [entry for entry in timeline.get("dates") or [] if isinstance(entry, dict)]
        return StreetViewDates(dates=sorted(dates, key=_captured_on, reverse=True), complete=bool(timeline.get("complete", True)))

    return coalesced(f"redata:street-view:{point_key(latitude, longitude)}", ask, ttl=SHARE_SECONDS)


def _captured_on(entry: dict[str, Any]) -> str:
    return str(entry.get("captured_on") or "")


def dates_from_captures(captures: list[dict[str, Any]], latitude: float, longitude: float) -> list[dict[str, Any]]:
    """Group capture rows into the timeline's per-network, per-date entries, as REData's own timeline does.

    Each date's representative is the frame nearest the point, a panorama winning a tie: an arbitrary frame from a
    passing vehicle points down the street about half the time.

    Args:
        captures: ``StreetViewCaptureSerializer`` rows.
        latitude: The point's latitude.
        longitude: The point's longitude.

    Returns:
        ``{captured_on, provider, count, is_panoramic, representative}`` entries, newest first; undated captures are
        left out, as the timeline leaves them out of its dates.
    """
    from urbanlens.dashboard.services.geo.distance import haversine_meters

    def distance(capture: dict[str, Any]) -> float:
        capture_latitude, capture_longitude = capture.get("latitude"), capture.get("longitude")
        if not isinstance(capture_latitude, int | float) or not isinstance(capture_longitude, int | float):
            return float("inf")
        return haversine_meters(latitude, longitude, float(capture_latitude), float(capture_longitude))

    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for capture in captures:
        captured_on = str(capture.get("captured_on") or "")[:10]
        provider = str(capture.get("provider") or "")
        if captured_on and provider:
            groups.setdefault((provider, captured_on), []).append(capture)
    dates = [
        {
            "captured_on": captured_on,
            "provider": provider,
            "count": len(members),
            "is_panoramic": any(member.get("is_panoramic") for member in members),
            "representative": min(members, key=lambda member: (distance(member), not member.get("is_panoramic"))),
        }
        for (provider, captured_on), members in groups.items()
    ]
    return sorted(dates, key=_captured_on, reverse=True)


def reference_documents_near(latitude: float, longitude: float) -> LocationContextEnvelope:
    """The Wikipedia articles and Wikidata entities REData places near a point.

    Args:
        latitude: WGS-84 latitude.
        longitude: WGS-84 longitude.

    Returns:
        The envelope.

    Raises:
        LocationContextUnavailableError: No source answered, or the request failed.
    """

    def ask() -> LocationContextEnvelope:
        envelope = settled_domain(latitude, longitude, "reference_documents")
        if envelope is not None:
            return envelope
        from urbanlens.dashboard.services.apis.locations.redata_reference_documents_gateway import RedataReferenceDocumentsGateway

        return RedataReferenceDocumentsGateway().near(latitude, longitude)

    return coalesced(f"redata:reference-documents:{point_key(latitude, longitude)}", ask, ttl=SHARE_SECONDS)


def historical_features_near(latitude: float, longitude: float) -> LocationContextEnvelope:
    """Every mapped historical feature REData has near a point, with its geometry and validity years.

    Args:
        latitude: WGS-84 latitude.
        longitude: WGS-84 longitude.

    Returns:
        The envelope.

    Raises:
        LocationContextUnavailableError: The source failed to answer, or the request failed.
    """

    def ask() -> LocationContextEnvelope:
        envelope = settled_domain(latitude, longitude, "historical_features")
        if envelope is not None:
            return envelope
        from urbanlens.dashboard.services.apis.locations.redata_historical_features_gateway import RedataHistoricalFeaturesGateway

        return RedataHistoricalFeaturesGateway().get_historical_features(latitude, longitude)

    return coalesced(f"redata:historical-features:{point_key(latitude, longitude)}", ask, ttl=SHARE_SECONDS)


__all__ = [
    "CONTEXT_DOMAINS",
    "StreetViewDates",
    "dates_from_captures",
    "historical_features_near",
    "media_near",
    "reference_documents_near",
    "settled_domain",
    "street_view_dates",
]
