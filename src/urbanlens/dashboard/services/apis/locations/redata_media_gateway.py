"""REData-backed street-view carousel providers (Mapillary, KartaView, Panoramax).

REData's ``GET /api/v1/media/lookup/`` (``../REData/docs/api-reference.md``, "Media")
now fronts these three crowdsourced street-level imagery networks worldwide, each
fixed at a 100m search radius on REData's own side regardless of what
``radius_meters`` is sent. UrbanLens used to call each network directly
(``mapillary.py``/``kartaview.py``/``panoramax.py``, now deleted) with its own
auth/parsing quirks; :class:`RedataMediaGateway` replaces all three call sites with
one REData client, and the three :class:`StreetViewProvider` subclasses below are
thin wrappers that differ only in which ``?provider=`` tag they pass and their
display name - REData is now the only outbound caller, so there is no fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.services.apis.locations.base import StreetViewProvider, StreetViewSlide
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import RedataLocationContextGateway

if TYPE_CHECKING:
    from collections.abc import Generator

_MEDIA_LOOKUP_PATH = "/api/v1/media/lookup/"


@dataclass(slots=True, kw_only=True)
class RedataMediaGateway(RedataLocationContextGateway):
    """REST client for REData's ``/api/v1/media/lookup/`` near-point endpoint.

    Shares one outbound rate-limit bucket (``redata_media``) across every media
    lookup UrbanLens makes through REData, regardless of which upstream
    ``provider`` tag was requested - REData is the actual caller of
    Mapillary/KartaView/Panoramax now, and pools its own outbound budget for
    them server-side (see the base gateway's docstring), so the meaningful unit
    to rate-limit from UrbanLens's side is "calls to REData's media endpoint",
    not one bucket per upstream network.
    """

    service_key: ClassVar[str] = "redata_media"

    def lookup(
        self,
        latitude: float,
        longitude: float,
        *,
        kind: str | list[str] | None = None,
        provider: str | list[str] | None = None,
        radius_meters: float | None = None,
        limit: int | None = None,
        force_refresh: bool = False,
    ) -> list[dict[str, Any]]:
        """Look up media items near a coordinate.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            kind: Restrict to one or more ``MediaItemKind`` tags (e.g.
                ``"photo"``) - repeatable, matching REData's ``?kind=``.
            provider: Restrict to one or more provider tags (e.g.
                ``"mapillary"``) - repeatable, matching REData's ``?provider=``.
            radius_meters: Search radius in meters. Mapillary/KartaView/Panoramax
                are fixed at 100m on REData's own side regardless of this value;
                harmless to pass for the other registered media providers.
            limit: Bounded positive integer (REData caps at 200).
            force_refresh: Bypass REData's cache and re-query live.

        Returns:
            The envelope's ``results`` list, provider-tagged dicts per REData's
            ``MediaItemSerializer`` shape (``provider``, ``external_id``,
            ``kind``, ``title``, ``description``, ``url``, ``thumbnail_url``,
            ``credit``, ``latitude``, ``longitude``, ``attributes``, ...).
            Empty when nothing is nearby - see :meth:`near_point` for when this
            raises instead.
        """
        extra_params: dict[str, Any] | None = {"kind": kind} if kind is not None else None
        envelope = self.near_point(
            _MEDIA_LOOKUP_PATH,
            latitude,
            longitude,
            radius_meters=radius_meters,
            provider=provider,
            force_refresh=force_refresh,
            limit=limit,
            extra_params=extra_params,
        )
        return envelope.results


def _capture_date(item: dict[str, Any]) -> str:
    """Best-effort human-readable capture date for a REData media row.

    REData's generic media-item contract promotes no capture-date column of
    its own (only ``record_retrieved_at``/``created``/``updated``, timestamps
    for when REData itself last touched the row) - a per-source capture date,
    if the source publishes one, would only ever appear inside the verbatim
    ``attributes`` bag. Checked defensively since the exact key REData uses
    there (if any) isn't part of the documented contract; falls back to
    REData's own record timestamp, which is at least a real date rather than
    a fabricated one.
    """
    attributes = item.get("attributes") or {}
    captured = attributes.get("captured_at") or attributes.get("date") or item.get("record_retrieved_at") or item.get("created") or ""
    return str(captured)[:10] or "Unknown"


@dataclass(slots=True, kw_only=True)
class _RedataStreetViewProvider(StreetViewProvider):
    """Base for one REData ``media/lookup`` provider surfaced in the street-view carousel.

    Subclasses set ``_redata_provider`` (REData's own ``?provider=`` tag) and
    ``_display_name``. ``service_key`` stays each provider's own historical tag
    (``mapillary``/``kartaview``/``panoramax``) rather than sharing
    :class:`RedataMediaGateway`'s - it namespaces this provider's own 24h slide
    cache (see ``StreetViewProvider.get_street_view_slides``) and the debug
    overlay's per-provider breakdown, both of which must stay distinct per
    network even though the actual HTTP call is now made (and rate-limited)
    through one shared REData gateway instance.

    A REData failure is deliberately left to propagate out of
    :meth:`_generate_street_view_slides` rather than being caught here - the
    street-view carousel's collector (``collect_street_view_slides``) already
    tolerates any one provider raising, logging it and recording
    ``ProviderFetchResult(..., ok=False)`` for the admin debug overlay instead
    of silently returning an empty, indistinguishable-from-no-coverage result.
    """

    _redata_provider: ClassVar[str] = ""
    _display_name: ClassVar[str] = ""

    def _generate_street_view_slides(self, latitude: float, longitude: float, *, radius: float = 50, limit: int = 5) -> Generator[StreetViewSlide]:
        """Yield street-level photos from this provider, via REData.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            radius: Search radius in meters - see :meth:`RedataMediaGateway.lookup`.
            limit: Maximum number of slides to request from REData.

        Yields:
            ``StreetViewSlide`` entries in the order REData returns them.
        """
        gateway = RedataMediaGateway()
        results = gateway.lookup(latitude, longitude, kind="photo", provider=self._redata_provider, radius_meters=radius, limit=limit)
        for item in results:
            img_src = item.get("url") or item.get("thumbnail_url")
            if not img_src:
                continue
            attributes = item.get("attributes") or {}
            heading = attributes.get("heading_degrees")
            yield StreetViewSlide(
                img_src=img_src,
                source=self._display_name,
                date=_capture_date(item),
                heading=float(heading) if heading is not None else None,
                latitude=item.get("latitude"),
                longitude=item.get("longitude"),
            )


@dataclass(slots=True, kw_only=True)
class MapillaryStreetViewProvider(_RedataStreetViewProvider):
    """Mapillary crowdsourced street-level imagery, via REData."""

    service_key: ClassVar[str] = "mapillary"
    paid_service: ClassVar[bool] = False
    _redata_provider: ClassVar[str] = "mapillary"
    _display_name: ClassVar[str] = "Mapillary"


@dataclass(slots=True, kw_only=True)
class KartaViewStreetViewProvider(_RedataStreetViewProvider):
    """KartaView crowdsourced street-level imagery, via REData."""

    service_key: ClassVar[str] = "kartaview"
    paid_service: ClassVar[bool] = False
    _redata_provider: ClassVar[str] = "kartaview"
    _display_name: ClassVar[str] = "KartaView"


@dataclass(slots=True, kw_only=True)
class PanoramaxStreetViewProvider(_RedataStreetViewProvider):
    """Panoramax crowdsourced street-level imagery, via REData."""

    service_key: ClassVar[str] = "panoramax"
    paid_service: ClassVar[bool] = False
    _redata_provider: ClassVar[str] = "panoramax"
    _display_name: ClassVar[str] = "Panoramax"
