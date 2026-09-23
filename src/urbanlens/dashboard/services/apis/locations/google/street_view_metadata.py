"""Whether Google has Street View imagery near a point, from the no-charge metadata endpoint."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar
from urllib.parse import urlsplit

from django.core.cache import cache

from urbanlens.core.cache_keys import make_cache_key
from urbanlens.dashboard.services.apis.locations.base import external_data_cache_seconds
from urbanlens.dashboard.services.core.gateway import Gateway, GatewayRequestError

METADATA_URL = "https://maps.googleapis.com/maps/api/streetview/metadata"

#: About 11m, well inside the metadata endpoint's 50m default search radius.
COORDINATE_DECIMALS = 4

_NO_IMAGERY = frozenset({"ZERO_RESULTS", "NOT_FOUND"})


def _unrestricted_api_key() -> str:
    from urbanlens.UrbanLens.settings.app import settings

    return settings.google_unrestricted_api_key or ""


@dataclass(kw_only=True)
class GoogleStreetViewMetadataGateway(Gateway):
    """Street View coverage lookups, ledgered and rate limited apart from billed Street View images.

    Attributes:
        api_key: A server-side Google key; the metadata endpoint rejects referrer-restricted ones.
    """

    service_key: ClassVar[str] = "google_street_view_metadata"

    api_key: str = field(default_factory=_unrestricted_api_key)

    @staticmethod
    def endpoint_for_log(url: str) -> str:
        """Record the path only, so neither the key nor the coordinate reaches ``ApiCallLog``."""
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}{parts.path}"

    def has_imagery(self, latitude: float, longitude: float) -> bool:
        """Whether outdoor Street View imagery exists near a point.

        The point is rounded before it is sent, and the answer is cached per rounded point, so
        repeated right-clicks on one spot ask Google once.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.

        Returns:
            True when Google reports a panorama, False when it reports none.

        Raises:
            GatewayRequestError: Google refused or failed the request; not cached.
            RequestCancelledError: The rate limiter or the outbound-call policy refused it.
            requests.RequestException: The request did not complete.
        """
        lat = round(latitude, COORDINATE_DECIMALS)
        lng = round(longitude, COORDINATE_DECIMALS)
        cache_key = make_cache_key("street_view_metadata", f"{lat:.{COORDINATE_DECIMALS}f}", f"{lng:.{COORDINATE_DECIMALS}f}")
        cached = cache.get(cache_key)
        if isinstance(cached, bool):
            return cached

        response = self.session.get(
            METADATA_URL,
            params={"location": f"{lat},{lng}", "key": self.api_key, "source": "outdoor"},
            timeout=4,
        )
        response.raise_for_status()
        try:
            status = response.json().get("status", "")
        except (ValueError, AttributeError) as exc:
            raise GatewayRequestError("Street View metadata returned an unreadable body") from exc

        if status == "OK":
            available = True
        elif status in _NO_IMAGERY:
            available = False
        else:
            raise GatewayRequestError(f"Street View metadata API error: {status}")

        cache.set(cache_key, available, external_data_cache_seconds())
        return available
