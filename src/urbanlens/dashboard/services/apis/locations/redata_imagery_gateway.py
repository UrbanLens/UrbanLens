"""Gateway for REData's ``/imagery/`` endpoint - pictures of a place.
Backs :class:`~urbanlens.dashboard.plugins.builtin.satellite_imagery.RedataSatelliteProvider`, which requests only the providers not already covered - more richly - by UrbanLens's own direct Esri integration (current + historical Wayback releases) and its separate USGS Historical Topo Maps panel; see that module for exactly which REData imagery providers are requested and why."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import (
    REASON_SOURCE_ERROR,
    LocationContextUnavailableError,
    RedataLocationContextGateway,
)

if TYPE_CHECKING:
    import datetime

logger = logging.getLogger(__name__)

_IMAGERY_PATH = "/api/v1/imagery/"
_TIMELINE_PATH = "/api/v1/imagery/timeline/"
_CAPTURE_PATH = "/api/v1/imagery/capture/"
_DOWNLOAD_TIMEOUT = 30

#: `POST /imagery/capture/` reasons that mean "there is no image for this exact date" rather than a
#: failure - one caught before the fetch (`date_unavailable`: outside the layer's published
#: intervals) and one after (`no_imagery`: the source answered and had nothing that day, a permanent
#: answer per REData's docs, not one to retry).
_NO_IMAGE_REASONS = frozenset({"date_unavailable", "no_imagery"})


class RedataImageryGateway(RedataLocationContextGateway):
    """REST client for REData's cross-provider imagery endpoint."""

    service_key: ClassVar[str] = "redata_imagery"

    def get_imagery(self, latitude: float, longitude: float, *, providers: list[str] | None = None) -> list[dict[str, Any]]:
        """Return REData's normalized imagery results for a coordinate.

        Args:
                latitude: WGS-84 latitude.
                longitude: WGS-84 longitude.
                providers: Restrict to these REData provider tags; omit for
                every provider REData has configured.

        Returns:
                Provider-tagged imagery result dicts (``provider``, ``kind``,
                ``url``, ``delivery``, ``captured_on``, ``captured_label``,
                ``attribution``, and an ``attributes`` blob that carries
                ``subdomains`` for a ``tile_template`` delivery) - empty when
                nothing answered.

        Raises:
                LocationContextUnavailableError: Every requested provider failed
                to answer, or the request to REData failed outright."""
        envelope = self.near_point(_IMAGERY_PATH, latitude, longitude, provider=providers)
        return envelope.results

    def get_timeline(self, latitude: float, longitude: float, *, trigger_archive: bool = False) -> dict[str, Any]:
        """Return which dates imagery exists for at a coordinate.
        Two shapes come back and both matter, because sources answer differently and neither can be expressed as the other:

        Args:
                latitude: WGS-84 latitude.
                longitude: WGS-84 longitude.
                trigger_archive: Use ``POST``, which re-queries every source live
                and queues permanent archiving of what it finds. REData
                documents this as the call to make when about to show a time
                slider; the ``GET`` never archives. Costs a live fetch, so it
                is off by default.

        Returns:
                The timeline envelope (``earliest``, ``latest``, ``years``,
                ``captures``, ``providers_timeline``, ``providers``), or an empty
                dict when nothing answered.

        Raises:
                LocationContextUnavailableError: The request to REData failed."""
        params = {"lat": latitude, "lng": longitude}
        if trigger_archive:
            return self.post_json(_TIMELINE_PATH, params) or {}
        return self.get_json(_TIMELINE_PATH, params) or {}

    def download_bytes(self, url: str) -> bytes:
        """Fetch a credentialed imagery source's bytes through REData's authenticated proxy.

        Args:
                url: The ``url`` field from an imagery result whose ``delivery``
                needs REData's own auth (the three keyed providers - see the
                module docstring) - either absolute or relative to ``base_url``.

        Returns:
                The raw image bytes.

        Raises:
                LocationContextUnavailableError: The request failed, or REData
                answered with a non-200 status."""
        base_url = self.base_url
        if base_url is None:
            raise LocationContextUnavailableError(REASON_SOURCE_ERROR, "UL_REDATA_API_URL is not configured.")
        target = url if url.startswith(("http://", "https://")) else f"{base_url.rstrip('/')}/{url.lstrip('/')}"
        try:
            response = self.session.get(target, headers=self._headers, timeout=_DOWNLOAD_TIMEOUT)
        except OSError as exc:
            raise LocationContextUnavailableError(REASON_SOURCE_ERROR, f"Could not reach REData: {exc}") from exc
        if response.status_code != 200:
            self._raise_for_error_status(response, url)
        return response.content

    def download_archived_copy(self, asset_uuid: str, *, width: int | None = None, height: int | None = None) -> bytes:
        """Fetch REData's own composed, permanently archived copy of an imagery asset.
        Rendered on first request and served from disk on every later one, so ``width``/``height`` only take effect the first time a given asset is downloaded.

        Args:
                asset_uuid: The imagery result's own ``uuid`` field (not its
                ``provider`` tag).
                width: Composed image width in pixels, applied only on the first
                download of this asset.
                height: Composed image height in pixels, applied only on the
                first download of this asset.

        Returns:
                The raw image bytes.

        Raises:
                LocationContextUnavailableError: The request failed, or REData
                answered with a non-200 status - including a ``time_series``
                row with no date materialized yet (``400 date_required``;
                see :meth:`capture_time_series`) or no image for this asset
                (``404 no_imagery``)."""
        base_url = self.base_url
        if base_url is None:
            raise LocationContextUnavailableError(REASON_SOURCE_ERROR, "UL_REDATA_API_URL is not configured.")
        params: dict[str, int] = {}
        if width is not None:
            params["width"] = width
        if height is not None:
            params["height"] = height
        path = f"/api/v1/imagery/{asset_uuid}/download/"
        target = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
        try:
            response = self.session.get(target, params=params, headers=self._headers, timeout=_DOWNLOAD_TIMEOUT)
        except OSError as exc:
            raise LocationContextUnavailableError(REASON_SOURCE_ERROR, f"Could not reach REData: {exc}") from exc
        if response.status_code != 200:
            self._raise_for_error_status(response, path)
        return response.content

    def capture_time_series(self, asset_uuid: str, date: datetime.date, *, width: int = 1024, height: int = 1024) -> dict[str, Any] | None:
        """Materialize one date from a continuous (``time_series``) imagery source.
        Only meaningful for a result whose ``delivery`` is ``time_series`` (today, only ``nasa_gibs``) - its ``url`` is a range, not a picture, and this is what turns one date in that range into a real, permanently stored image.

        Args:
                asset_uuid: The ``time_series`` result's own ``uuid`` - the layer
                to materialize a date from.
                date: The date to materialize. Must fall inside one of the
                layer's own published intervals.
                width: Rendered image width in pixels.
                height: Rendered image height in pixels.

        Returns:
                The materialized result row (same shape as a ``GET /imagery/``
                result - pass its ``uuid`` to :meth:`download_archived_copy` to
                fetch the bytes), or ``None`` when REData reports there is no
                image for this exact date - either because it falls outside the
                layer's published intervals (``date_unavailable``) or because the
                source answered and had nothing that day (``no_imagery``,
                permanent - do not retry). Both are documented, expected
                outcomes, not failures.

        Raises:
                LocationContextUnavailableError: The request failed outright, or
                REData reports a transient outage (``imagery_unavailable``/
                ``rate_limited``) - retryable, but not by this call."""
        base_url = self.base_url
        if base_url is None:
            raise LocationContextUnavailableError(REASON_SOURCE_ERROR, "UL_REDATA_API_URL is not configured.")
        body = {"asset_uuid": asset_uuid, "date": date.isoformat(), "width": width, "height": height}
        try:
            response = self.session.post(f"{base_url.rstrip('/')}{_CAPTURE_PATH}", json=body, headers=self._headers, timeout=_DOWNLOAD_TIMEOUT)
        except OSError as exc:
            raise LocationContextUnavailableError(REASON_SOURCE_ERROR, f"Could not reach REData: {type(exc).__name__}") from exc

        if response.status_code == 200:
            try:
                parsed = response.json()
            except ValueError as exc:
                raise LocationContextUnavailableError(REASON_SOURCE_ERROR, "REData returned an unparseable response.") from exc
            if not isinstance(parsed, dict):
                raise LocationContextUnavailableError(REASON_SOURCE_ERROR, "REData returned an unexpected response shape.")
            return parsed

        try:
            error_body = response.json()
        except ValueError:
            error_body = {}
        reason = error_body.get("error") or REASON_SOURCE_ERROR
        # 400/404 share one shape with every other REData error (`{"error", "message"}`) but the
        # base gateway's generic error handling only inspects the body for 400/503 - 404 is a
        # documented, structured answer here (`no_imagery`), not the "unexpected status" case that
        # warrants a warning log, so it is parsed the same way here rather than reused from
        if response.status_code in (400, 404) and reason in _NO_IMAGE_REASONS:
            return None
        if response.status_code in (400, 404, 503):
            raise LocationContextUnavailableError(reason, error_body.get("message", ""))
        logger.warning("REData imagery capture request failed (%s): %s", response.status_code, response.text[:500])
        raise LocationContextUnavailableError(REASON_SOURCE_ERROR, f"REData request failed with status {response.status_code}.")
