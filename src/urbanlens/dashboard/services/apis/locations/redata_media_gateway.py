"""REData media client plus the street-view carousel providers (Mapillary, KartaView, Panoramax).

:class:`RedataMediaGateway` wraps ``GET /api/v1/media/lookup/``
(``../REData/docs/api-reference.md``, "Media") - each network's current
photos near a point. The three :class:`StreetViewProvider` subclasses below
used to draw from it, but now source their slides from REData's
``/street-view/timeline/`` (see ``redata_street_view_gateway``) instead: one
dated slide per capture date rather than an undated handful of recent
frames. UrbanLens used to call each network directly
(``mapillary.py``/``kartaview.py``/``panoramax.py``, now deleted) with its
own auth/parsing quirks; REData is now the only outbound caller, so there is
no fallback.
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
        is_aerial: bool = False,
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
            is_aerial: Only drone/aerial footage, recognised by REData from
                each item's own title and description.
            force_refresh: Bypass REData's cache and re-query live.

        Returns:
            The envelope's ``results`` list, provider-tagged dicts per REData's
            ``MediaItemSerializer`` shape (``provider``, ``external_id``,
            ``kind``, ``title``, ``description``, ``url``, ``thumbnail_url``,
            ``credit``, ``latitude``, ``longitude``, ``attributes``, ...).
            Empty when nothing is nearby - see :meth:`near_point` for when this
            raises instead.
        """
        extra_params: dict[str, Any] = {}
        if kind is not None:
            extra_params["kind"] = kind
        if is_aerial:
            extra_params["is_aerial"] = "true"
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
        """Yield one dated slide per capture *date* from this provider, newest first.

        Sourced from REData's ``/street-view/timeline/`` rather than
        ``/media/lookup/``: the timeline holds every date a camera passed the
        point (not just each network's current nearby photos), and its
        ``representative`` is the frame taken nearest the query point - an
        arbitrary pick shows a picture down the street about half the time.
        The result is a decay progression: the same site on every date it was
        photographed.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            radius: Unused - REData pins the street-view search at 100 m per
                provider; kept for the ``StreetViewProvider`` signature.
            limit: Maximum number of dated slides to yield.

        Yields:
            ``StreetViewSlide`` entries, newest capture date first.
        """
        from urbanlens.dashboard.services.apis.locations.redata_street_view_gateway import RedataStreetViewGateway

        timeline = RedataStreetViewGateway().get_timeline(latitude, longitude, provider=self._redata_provider)
        dates = sorted(timeline.get("dates") or [], key=lambda entry: entry.get("captured_on") or "", reverse=True)
        for entry in dates:
            representative = entry.get("representative") or {}
            # download_url (REData's archived copy) needs API auth, so the
            # browser gets the network's own copy - which attribution
            # requires linking anyway.
            img_src = representative.get("image_url") or representative.get("thumbnail_url")
            if not img_src:
                continue
            heading = representative.get("heading_degrees")
            yield StreetViewSlide(
                img_src=img_src,
                source=self._display_name,
                date=str(entry.get("captured_on") or "")[:10] or "Unknown",
                heading=float(heading) if heading is not None else None,
                latitude=representative.get("latitude"),
                longitude=representative.get("longitude"),
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
