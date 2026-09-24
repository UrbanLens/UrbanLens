"""Gateway for REData's Google Places API (New) endpoints."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import logging
from typing import Any, ClassVar

from urbanlens.dashboard.services.core.coalesce import coalesced
from urbanlens.dashboard.services.core.gateway import Gateway, GatewayRateLimitedError, GatewayRequestError, read_capped
from urbanlens.UrbanLens.settings.app import settings

logger = logging.getLogger(__name__)

#: REData's own error code for "Places API (New) request budget is exhausted right now" - a known,
#: expected, self-clearing condition (REData enforces its own budget against Google, independent of
#: anything in this codebase's rate_limiter), not a bug worth a traceback.
#: Always paired with a 503.
_RATE_LIMITED_ERROR = "rate_limited"


def _is_rate_limited_body(body: Any) -> bool:
    return isinstance(body, dict) and body.get("error") == _RATE_LIMITED_ERROR


def _rows(body: Any) -> list[dict[str, Any]]:
    """Unwrap REData's ``{"count", "results"}`` collection envelope.
    All three of these endpoints answer through REData's ``collection_response`` helper, so the rows are under ``results`` - never a bare array.

    Args:
        body: The decoded response body.

    Returns:
        The row dicts, possibly empty."""
    if isinstance(body, dict):
        body = body.get("results")
    return [row for row in body if isinstance(row, dict)] if isinstance(body, list) else []


_REQUEST_TIMEOUT = 30
#: Name resolution, the Places photo panel and photo enrichment each ask the same question about one pin.
_SHARED_SECONDS = 60 * 60


def _shared_key(kind: str, params: dict[str, Any]) -> str:
    """The shared-answer key for one Places question."""
    return f"redata:{kind}:{hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()}"


@dataclass(slots=True, kw_only=True)
class RedataPlacesGateway(Gateway):
    """REST client for REData's ``/places/...`` endpoints."""

    service_key: ClassVar[str] = "redata_places"
    paid_service: ClassVar[bool] = False

    # default_factory so settings changes apply per instance; a bare default freezes at import.
    base_url: str | None = field(default_factory=lambda: settings.redata_api_url)
    api_key: str | None = field(default_factory=lambda: settings.redata_api_key)

    def __post_init__(self) -> None:
        Gateway.__post_init__(self)
        if not self.base_url:
            raise ValueError("UL_REDATA_API_URL must be configured.")
        if not self.base_url.startswith(("http://", "https://")):
            self.base_url = f"https://{self.base_url}"
        if not self.api_key:
            raise ValueError("UL_REDATA_API_KEY must be configured.")

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}

    def _request(self, path: str, *, params: dict[str, Any] | None = None, stream: bool = False) -> Any:
        """GET one REData endpoint and return the raw ``requests.Response``.

        Args:
            path: Path relative to ``base_url`` (leading slash optional).
            params: Query-string parameters, if any.
            stream: Leave the body unread, for a download read through ``read_capped``.

        Returns:
            The raw ``requests.Response`` - callers interpret their own endpoint's status codes, since 404 means "confirmed nothing here" on some endpoints (``get_place``/``download_photo``) but would be a routing bug on others (the search/autocomplete endpoints).

        Raises:
            GatewayRequestError: The request itself could not be made (network failure) - never raised for a completed HTTP response, regardless of status code.
        """
        base_url = self.base_url
        if base_url is None:
            # __post_init__ already validates this for the normal construction
            # path; this only narrows the type for mypy.
            raise GatewayRequestError("UL_REDATA_API_URL is not configured.")
        try:
            return self.session.get(f"{base_url.rstrip('/')}/{path.lstrip('/')}", params=params, headers=self._headers, timeout=_REQUEST_TIMEOUT, stream=stream)
        except OSError as exc:
            raise GatewayRequestError(f"Could not reach REData: {exc}") from exc

    def _error_for(self, response: Any, message: str) -> GatewayRequestError:
        """Build the exception a failed ``response`` should raise.

        Args:
            response: The non-success ``requests.Response`` being reported.
            message: The exception message.

        Returns:
            :class:`~urbanlens.dashboard.services.core.gateway.GatewayRateLimitedError` when REData's body identifies its own exhausted request budget, else the plain, less specific ``GatewayRequestError``.
        """
        try:
            body = response.json()
        except ValueError:
            body = None
        if _is_rate_limited_body(body):
            return GatewayRateLimitedError(message)
        return GatewayRequestError(message)

    def get_place(self, place_id: str) -> dict[str, Any] | None:
        """Fetch (and, on REData's end, permanently cache) one place's full details.

        Args:
            place_id: Google's own ``place_id``.

        Returns:
            The place dict (see ``api-reference.md`` for the full field list - flat ``latitude``/``longitude``, ``formatted_address``, ``google_maps_uri``, ``types``, ``photos`` metadata, and ``photo_records`` for :meth:`download_photo`), or None when Google...

        Raises:
            GatewayRequestError: The request failed outright, or REData reported a transient failure (rate-limited, unreachable Google, or REData's own API key not configured).
        """

        def details() -> dict[str, Any] | None:
            response = self._request(f"/api/v1/places/{place_id}/")
            if response.status_code == 200:
                return dict(response.json())
            if response.status_code == 404:
                return None
            logger.warning("REData place details fetch failed (%s): %s", response.status_code, response.text[:500])
            raise self._error_for(response, f"REData place details request failed with status {response.status_code}.")

        return coalesced(_shared_key("place", {"place_id": place_id}), details, ttl=_SHARED_SECONDS)

    def search_nearby(self, latitude: float, longitude: float, radius_meters: float = 200, included_types: list[str] | None = None, max_results: int = 20) -> list[dict[str, Any]]:
        """Search places near a coordinate via REData.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            radius_meters: Search radius in meters.
            included_types: Google place-type taxonomy filter; omit to search
                every type.
            max_results: Capped by Google at 20 (no pagination beyond one page).

        Returns:
            A list of lightweight place-projection dicts (``place_id``, ``name``, ``formatted_address``, ``latitude``, ``longitude``, ``types``, ``primary_type``, ``business_status``, ``google_maps_uri``) - possibly empty.

        Raises:
            GatewayRequestError: The request failed outright.
        """
        params: dict[str, Any] = {"latitude": latitude, "longitude": longitude, "radius_meters": radius_meters, "max_results": max_results}
        if included_types:
            params["included_type"] = list(included_types)

        def search() -> list[dict[str, Any]]:
            response = self._request("/api/v1/places/search/nearby/", params=params)
            if response.status_code != 200:
                logger.warning("REData nearby places search failed (%s): %s", response.status_code, response.text[:500])
                raise self._error_for(response, f"REData nearby places search failed with status {response.status_code}.")
            return _rows(response.json())

        return coalesced(_shared_key("places-nearby", {**params, "latitude": round(latitude, 6), "longitude": round(longitude, 6)}), search, ttl=_SHARED_SECONDS)

    def search_text(self, query: str, latitude: float | None = None, longitude: float | None = None, radius_meters: float = 200, max_results: int = 20) -> list[dict[str, Any]]:
        """Free-text place search via REData.

        Args:
            query: Free text - an address, business name, or "coffee near Main St".
            latitude: Optional bias center.
            longitude: Optional bias center.
            radius_meters: Only meaningful with latitude/longitude.
            max_results: Capped by Google at 20.

        Returns:
            Same shape as :meth:`search_nearby` - possibly empty.

        Raises:
            GatewayRequestError: The request failed outright.
        """
        params: dict[str, Any] = {"query": query, "radius_meters": radius_meters, "max_results": max_results}
        if latitude is not None:
            params["latitude"] = latitude
        if longitude is not None:
            params["longitude"] = longitude
        response = self._request("/api/v1/places/search/text/", params=params)
        if response.status_code != 200:
            logger.warning("REData text places search failed (%s): %s", response.status_code, response.text[:500])
            raise self._error_for(response, f"REData text places search failed with status {response.status_code}.")
        return _rows(response.json())

    def autocomplete(self, query: str, latitude: float | None = None, longitude: float | None = None, radius_meters: float = 200) -> list[dict[str, Any]]:
        """Predictions-as-you-type via REData.

        Args:
            query: The partial text typed so far.
            latitude: Optional bias center.
            longitude: Optional bias center.
            radius_meters: Only meaningful with latitude/longitude.

        Returns:
            A list of flattened prediction dicts (``kind``, ``place_id``, ``text``, ``main_text``, ``secondary_text``, ``types``) - ``place_id`` is ``""`` for a ``"query"``-kind suggestion.

        Raises:
            GatewayRequestError: The request failed outright.
        """
        params: dict[str, Any] = {"query": query, "radius_meters": radius_meters}
        if latitude is not None:
            params["latitude"] = latitude
        if longitude is not None:
            params["longitude"] = longitude
        response = self._request("/api/v1/places/autocomplete/", params=params)
        if response.status_code != 200:
            logger.warning("REData autocomplete failed (%s): %s", response.status_code, response.text[:500])
            raise self._error_for(response, f"REData autocomplete request failed with status {response.status_code}.")
        return _rows(response.json())

    def download_photo(self, place_id: str, photo_record_id: int) -> tuple[bytes, str] | None:
        """Download one photo's actual image bytes via REData.

        Args:
            place_id: The place's Google ``place_id``.
            photo_record_id: REData's own internal id for this photo (from
                the place's ``photo_records`` - see :meth:`get_place`), not
                Google's own photo resource name.

        Returns:
            Tuple of (file bytes, content-type), or None when REData has confirmed this photo is no longer available.

        Raises:
            GatewayRequestError: The request failed outright, REData reported a transient failure, or the photo is over the proxied-media cap.
        """
        response = self._request(f"/api/v1/places/{place_id}/photos/{photo_record_id}/download/", stream=True)
        if response.status_code == 200:
            return read_capped(response, what="REData place photo"), response.headers.get("Content-Type", "image/jpeg")
        if response.status_code == 404:
            return None
        logger.warning("REData photo download failed (%s): %s", response.status_code, response.text[:500])
        raise self._error_for(response, f"REData photo download failed with status {response.status_code}.")
