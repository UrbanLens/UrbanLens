"""REData media client plus the street-view carousel providers (Mapillary, KartaView, Panoramax)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.services.apis.locations.base import StreetViewProvider, StreetViewSlide
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import REASON_ALL_PROVIDERS_UNAVAILABLE, LocationContextUnavailableError, RedataLocationContextGateway

if TYPE_CHECKING:
    from collections.abc import Generator

_MEDIA_LOOKUP_PATH = "/api/v1/media/lookup/"


@dataclass(slots=True, kw_only=True)
class RedataMediaGateway(RedataLocationContextGateway):
    """REST client for REData's ``/api/v1/media/lookup/`` near-point endpoint."""

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

        Returns:
            The envelope's ``results`` list, provider-tagged dicts per REData's ``MediaItemSerializer`` shape (``provider``, ``external_id``, ``kind``, ``title``, ``description``, ``url``, ``thumbnail_url``, ``credit``, ``latitude``, ``longitude``, ``attributes``, ...)."""
        extra_params: dict[str, Any] = {}
        if kind is not None:
            extra_params["kind"] = kind
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
        if not envelope.complete and not envelope.results:
            # An outage rather than an answer, and callers cache what they get -
            # see services.pins.redata_panel for the same rule stated in full.
            raise LocationContextUnavailableError(REASON_ALL_PROVIDERS_UNAVAILABLE, "Every media source covering this point failed to answer.")
        if is_aerial:
            return [item for item in envelope.results if item.get("is_aerial")]
        return envelope.results


@dataclass(slots=True, kw_only=True)
class _RedataStreetViewProvider(StreetViewProvider):
    """Base for one REData ``media/lookup`` provider surfaced in the street-view carousel."""

    _redata_provider: ClassVar[str] = ""
    _display_name: ClassVar[str] = ""

    def _generate_street_view_slides(self, latitude: float, longitude: float, *, radius: float = 50, limit: int = 5) -> Generator[StreetViewSlide]:
        """Yield one dated slide per capture *date* from this provider, newest first.

        Yields:
            ``StreetViewSlide`` entries, newest capture date first."""
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
