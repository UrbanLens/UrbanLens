"""Yelp Fusion API gateway.

Every lookup is by coordinates or street address only - never by pin/wiki
name. A user's personal label for a place is not a reliable business name
(it might be a nickname, a physical description, or simply wrong), so
searching Yelp by it would routinely return an unrelated business. Yelp's
own location-based search exists for exactly this reason: ``/businesses/search``
accepts ``latitude``/``longitude`` or a free-text ``location`` address and
returns whatever business Yelp associates with that place, independent of
what anyone has privately named it.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, ClassVar

from urbanlens.dashboard.services.gateway import Gateway, GatewayRequestError

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.yelp.com/v3"
_REQUEST_TIMEOUT = 15
# A close-in radius keeps the match tied to the actual pinned building rather
# than picking up a nearby, unrelated business.
_SEARCH_RADIUS_METERS = 75


@dataclass(slots=True, kw_only=True)
class YelpGateway(Gateway):
    """REST client for the Yelp Fusion API.

    Attributes:
        api_key: The site's Yelp Fusion private API key.
    """

    service_key: ClassVar[str] = "yelp"
    paid_service: ClassVar[bool] = False

    api_key: str

    def __post_init__(self) -> None:
        Gateway.__post_init__(self)
        self.session.headers.update({"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"})

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = self.session.get(f"{_BASE_URL}{path}", params=params, timeout=_REQUEST_TIMEOUT)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            raise GatewayRequestError(f"Yelp request to {path} failed: {exc}") from exc

    def find_nearest_business(self, *, latitude: float | None = None, longitude: float | None = None, address: str | None = None) -> dict[str, Any] | None:
        """Find the Yelp business at or nearest a set of coordinates or an address.

        Coordinates are preferred (they don't depend on Yelp's own address
        parsing); ``address`` is only used as a fallback when coordinates are
        unavailable. Never pass a pin/wiki's user-given name here.

        Args:
            latitude: WGS-84 latitude of the pinned location.
            longitude: WGS-84 longitude of the pinned location.
            address: Street address fallback, used only when coordinates are absent.

        Returns:
            The closest business result (Yelp already sorts by distance for a
            coordinate search), or None when nothing was found or no usable
            query was available.
        """
        params: dict[str, Any] = {"limit": 5, "radius": _SEARCH_RADIUS_METERS, "sort_by": "distance"}
        if latitude is not None and longitude is not None:
            params["latitude"] = latitude
            params["longitude"] = longitude
        elif address:
            params["location"] = address
        else:
            return None

        data = self._get("/businesses/search", params)
        businesses = data.get("businesses") or []
        return businesses[0] if businesses else None

    def get_business(self, business_id: str) -> dict[str, Any]:
        """Fetch full details (rating, price, hours, photos, categories, ...) for a business."""
        return self._get(f"/businesses/{business_id}")

    def get_reviews(self, business_id: str) -> list[dict[str, Any]]:
        """Fetch up to 3 review excerpts for a business, Yelp Fusion's own cap."""
        data = self._get(f"/businesses/{business_id}/reviews", {"sort_by": "newest"})
        return data.get("reviews") or []
