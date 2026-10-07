"""REData's near-point domains, each read once per point and shared by every panel that shows it.

Several panels of one page read the same REData domain, each from its own task as the page opens: the street
carousel's three networks and the Street-level gallery, the Historical Features panel and the time slider, the Aerial
and Nearby Media galleries. Each domain is asked once per point here and the answer shared for a few minutes.

``GET /locations/context/`` is asked first. It answers all of :data:`CONTEXT_DOMAINS` in one call from what REData
already holds, charged to the key's default pool rather than the lookup pool the per-domain endpoints share. A domain
it cannot answer in full - a provider REData has not asked about this point yet - is then asked of its own endpoint.

Every REData media row and street-view capture reaches this site through :func:`media_near` or
:func:`street_view_dates`, so a row whose source image is gone (:func:`mirror_gone`) is left out here, on both paths and
before the answer is shared: no gallery tile, carousel slide, copy (``services.media.remote_copies``) or proxy link is
made for an image that will not load (P325).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
import hashlib
import logging
import re
from typing import TYPE_CHECKING, Any

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError, redata_configured
from urbanlens.dashboard.services.core.coalesce import coalesced

if TYPE_CHECKING:
    from collections.abc import Collection

    from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope

logger = logging.getLogger(__name__)

#: The domains read through the context endpoint, asked together.
CONTEXT_DOMAINS: tuple[str, ...] = ("media", "street_view", "reference_documents", "historical_features")

#: How long one answer is shared. The panels of one page all ask within it; the panels' own caches keep it after.
SHARE_SECONDS = 600

#: REData's error code for a ``?provider=`` tag it does not register, answered ``400``.
REASON_UNKNOWN_PROVIDER = "unknown_provider"
#: How REData's refusal lists the tags, ahead of the valid ones: ``Unknown provider(s): a, b. Valid: ...``.
_UNKNOWN_PROVIDERS = re.compile(r"Unknown provider\(s\):\s*([^.]*)\.")
#: Most rejected tags a warning names, which bounds the length of a log line built from REData's message.
_MAX_LOGGED_TAGS = 20


#: REData's mark on a media row or street-view capture whose source no longer has its image (``attributes.mirror_gone``).
MIRROR_GONE = "mirror_gone"


def mirror_gone(row: object) -> bool:
    """Whether REData marked a media row or street-view capture as having no image left at its source.

    REData sets ``attributes.mirror_gone`` (``{"status", "reason", "at"}``) once every image URL the source published for
    the row answered that the image does not exist (``reason: "missing"``), or KartaView's storage said it is archived
    where nobody can read it (``"archived"``). Such a row's ``thumbnail_url`` does not load, REData never mirrors it, and
    its download answers 404. The mark clears when a later search finds the row with different URLs.

    Args:
        row: A ``MediaItemSerializer`` or ``StreetViewCaptureSerializer`` row - or anything else, which is not gone.

    Returns:
        True only for a mapping whose ``attributes`` is a mapping holding a truthy ``mirror_gone``.
    """
    if not isinstance(row, Mapping):
        return False
    attributes = row.get("attributes")
    return isinstance(attributes, Mapping) and bool(attributes.get(MIRROR_GONE))


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


def media_key(latitude: float, longitude: float, providers: Collection[str] | None) -> str:
    """The shared-answer key for a point's media, which names the provider set the answer was asked of.

    Args:
        latitude: WGS-84 latitude.
        longitude: WGS-84 longitude.
        providers: The providers asked, or None for every one REData registers.

    Returns:
        A key an answer shared for one provider set is never read back under for another.
    """
    asked = hashlib.sha256(",".join(sorted(providers)).encode()).hexdigest()[:12] if providers else "all"
    return f"redata:media:{point_key(latitude, longitude)}:{asked}"


def _rejected_providers(error: LocationContextUnavailableError, asked: Collection[str]) -> list[str]:
    """The tags REData's ``unknown_provider`` refusal names, or every tag asked when its message names none."""
    named = _UNKNOWN_PROVIDERS.search(str(error))
    tags = [tag for tag in re.split(r"[\s,]+", named.group(1)) if tag] if named else []
    return tags[:_MAX_LOGGED_TAGS] or list(asked)


def media_near(latitude: float, longitude: float) -> LocationContextEnvelope:
    """The media items REData has near a point that the Nearby Media and Aerial tabs can show (``MediaItemSerializer`` rows).

    The street-level networks are not asked for: their captures are the Street-level tab's, read by date through
    :func:`street_view_dates`, and a row from them here would be dropped unseen after costing REData a search of the
    network. The context read cannot be narrowed, so a settled answer from it may still hold their rows; the tabs drop
    those.

    A provider REData has renamed or retired is refused as ``unknown_provider``, which would blank both tabs. That one
    refusal is retried once with no filter, and logged naming the tag; the unfiltered answer holds every row the filtered
    one would, so it is shared under the unfiltered key and under the filtered one too, which spares each reader another
    refusal. Any other refusal or failure is raised, as it always was.

    Rows whose image is gone at its source (:func:`mirror_gone`) are left out of every one of those answers before it is
    shared, whichever key it is shared under.

    Args:
        latitude: WGS-84 latitude.
        longitude: WGS-84 longitude.

    Returns:
        The answer (:func:`without_gone_media`); its rows are a floor rather than a total when it is not ``complete``.

    Raises:
        LocationContextUnavailableError: No provider answered.
    """
    from urbanlens.dashboard.services.apis.locations.redata_media_gateway import NEARBY_MEDIA_PROVIDERS, RedataMediaGateway

    providers = list(NEARBY_MEDIA_PROVIDERS)

    def ask() -> LocationContextEnvelope:
        envelope = settled_domain(latitude, longitude, "media")
        if envelope is None:
            envelope = look_up()
        return without_gone_media(envelope)

    def look_up() -> LocationContextEnvelope:
        try:
            return RedataMediaGateway().lookup_envelope(latitude, longitude, provider=providers)
        except LocationContextUnavailableError as exc:
            if not exc.rejected or exc.reason != REASON_UNKNOWN_PROVIDER:
                raise
            logger.warning(
                "REData's media lookup does not know provider(s) %s, so it is asked again with no provider filter. NEARBY_MEDIA_PROVIDERS in services.apis.locations.redata_media_gateway has drifted from REData's media providers.",
                _rejected_providers(exc, providers),
            )
        return coalesced(media_key(latitude, longitude, None), lambda: without_gone_media(RedataMediaGateway().lookup_envelope(latitude, longitude)), ttl=SHARE_SECONDS)

    return coalesced(media_key(latitude, longitude, providers), ask, ttl=SHARE_SECONDS)


def without_gone_media(envelope: LocationContextEnvelope) -> LocationContextEnvelope:
    """``envelope`` less the rows REData marked :func:`mirror_gone`, with ``count`` lowered to match.

    A new envelope rather than an edit: the context read's envelope is shared with every reader of the point.
    ``providers[].count`` stays as each source reported it - it is what the source answered, and is read only for whether
    it answered (``LocationContextEnvelope.unanswered_sources``).

    Args:
        envelope: A media answer (``MediaItemSerializer`` rows).

    Returns:
        The envelope without its gone rows; ``envelope`` itself when it has none.
    """
    kept = [row for row in envelope.results if not mirror_gone(row)]
    dropped = len(envelope.results) - len(kept)
    if not dropped:
        return envelope
    return replace(envelope, results=kept, count=max(envelope.count - dropped, 0))


@dataclass(frozen=True, slots=True)
class StreetViewDates:
    """The dates a point was photographed from the street, across every network.

    Attributes:
        dates: ``{captured_on, provider, count, is_panoramic, representative}`` per network and date, newest first;
            ``representative`` is a ``StreetViewCaptureSerializer`` row - the nearest frame whose image is not gone
            (:func:`mirror_gone`). A date with no such frame is left out.
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
        dates = [live for entry in timeline.get("dates") or [] if isinstance(entry, dict) and (live := live_timeline_date(entry, latitude, longitude)) is not None]
        return StreetViewDates(dates=sorted(dates, key=_captured_on, reverse=True), complete=bool(timeline.get("complete", True)))

    return coalesced(f"redata:street-view:{point_key(latitude, longitude)}", ask, ttl=SHARE_SECONDS)


def _captured_on(entry: dict[str, Any]) -> str:
    return str(entry.get("captured_on") or "")


def _nearest(members: list[dict[str, Any]], latitude: float, longitude: float) -> dict[str, Any]:
    """The frame nearest the point, a panorama winning a tie: an arbitrary frame from a passing vehicle points down the
    street about half the time."""
    from urbanlens.dashboard.services.geo.distance import haversine_meters

    def distance(capture: dict[str, Any]) -> float:
        capture_latitude, capture_longitude = capture.get("latitude"), capture.get("longitude")
        if not isinstance(capture_latitude, int | float) or not isinstance(capture_longitude, int | float):
            return float("inf")
        return haversine_meters(latitude, longitude, float(capture_latitude), float(capture_longitude))

    return min(members, key=lambda member: (distance(member), not member.get("is_panoramic")))


def dates_from_captures(captures: list[dict[str, Any]], latitude: float, longitude: float) -> list[dict[str, Any]]:
    """Group capture rows into the timeline's per-network, per-date entries, as REData's own timeline does.

    Each date's representative is the frame nearest the point, a panorama winning a tie. A frame whose image is gone
    (:func:`mirror_gone`) is left out before grouping, so it is never a date's picture or counted in it.

    Args:
        captures: ``StreetViewCaptureSerializer`` rows.
        latitude: The point's latitude.
        longitude: The point's longitude.

    Returns:
        ``{captured_on, provider, count, is_panoramic, representative}`` entries, newest first; undated captures are
        left out, as the timeline leaves them out of its dates.
    """
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for capture in captures:
        captured_on = str(capture.get("captured_on") or "")[:10]
        provider = str(capture.get("provider") or "")
        if captured_on and provider and not mirror_gone(capture):
            groups.setdefault((provider, captured_on), []).append(capture)
    dates = [
        {
            "captured_on": captured_on,
            "provider": provider,
            "count": len(members),
            "is_panoramic": any(member.get("is_panoramic") for member in members),
            "representative": _nearest(members, latitude, longitude),
        }
        for (provider, captured_on), members in groups.items()
    ]
    return sorted(dates, key=_captured_on, reverse=True)


def live_timeline_date(entry: dict[str, Any], latitude: float, longitude: float) -> dict[str, Any] | None:
    """One ``/street-view/timeline/`` date without the frames REData marked :func:`mirror_gone`.

    From REData 0.3.6 a date's representative is the nearest frame still there, and is gone only when every frame of
    the date is; before it, the nearest frame, gone or not. When the date names its frames (``captures``, sent only for
    ``include_captures=true``), the nearest one still there stands in, and ``count``, ``is_panoramic`` and ``captures``
    describe only the frames left. Without them a gone representative has no stand-in, so the date is left out: from
    0.3.6 because nothing of it loads, before it at the cost of any frames that would. The ``count`` of a kept date is
    REData's, gone frames included.

    Args:
        entry: A ``dates[]`` entry of the timeline.
        latitude: The point's latitude.
        longitude: The point's longitude.

    Returns:
        The entry, a copy with a stand-in representative, or None when it has no frame left to show.
    """
    gone_representative = mirror_gone(entry.get("representative"))
    captures = entry.get("captures")
    if not isinstance(captures, list):
        return None if gone_representative else entry
    frames = [capture for capture in captures if isinstance(capture, dict) and not mirror_gone(capture)]
    if len(frames) == len(captures) and not gone_representative:
        return entry
    if not frames:
        return None
    return {
        **entry,
        "count": len(frames),
        "is_panoramic": any(frame.get("is_panoramic") for frame in frames),
        "representative": _nearest(frames, latitude, longitude),
        "captures": frames,
    }


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
    "MIRROR_GONE",
    "StreetViewDates",
    "dates_from_captures",
    "historical_features_near",
    "live_timeline_date",
    "media_near",
    "mirror_gone",
    "reference_documents_near",
    "settled_domain",
    "street_view_dates",
    "without_gone_media",
]
