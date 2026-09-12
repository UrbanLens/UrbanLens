"""The "photos near this pin" half of the Immich picker, capped and cached."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.core.cache import cache

from urbanlens.dashboard.models.profile.model import _haversine_km

if TYPE_CHECKING:
    from urbanlens.dashboard.models.immich.model import ImmichAccount
    from urbanlens.dashboard.services.apis.immich import ImmichGateway
    from urbanlens.dashboard.services.apis.immich.gateway import MapMarker

#: How many of the nearest assets the picker will consider, at any radius.
#: Matches the marker caps the app's own maps use (``_MAP_PIN_LIMIT``, ``_PREVIEW_MAP_PIN_LIMIT``).
NEARBY_ASSET_LIMIT = 500

#: How long a pin's measured neighbourhood stays good.
#: Long enough to cover a session of switching radii and modes, short enough that a photo uploaded
#: to Immich shows up in the picker without the user wondering why it hasn't.
NEARBY_CACHE_SECONDS = 300


def _cache_key(account: ImmichAccount, point: tuple[float, float]) -> str:
    """Cache key for one account's neighbourhood around one point.
    Scoped to the account, not the profile, and stamped with the account's ``updated`` timestamp - reconnecting to a different server or rotating the key must not serve the previous server's library."""
    latitude, longitude = point
    # Microseconds, not seconds: reconnecting to a different server inside the
    # same second is exactly the case this stamp exists for.
    return f"immich:nearby:{account.pk}:{account.updated.timestamp():.6f}:{latitude:.5f}:{longitude:.5f}"


@dataclass(frozen=True, slots=True)
class Neighbourhood:
    """One pin's measured surroundings in an Immich library.

    Attributes:
        nearest: ``(distance in metres, marker)`` pairs, closest first.
        truncated: Whether the cap dropped anything."""

    nearest: list[tuple[float, MapMarker]]
    truncated: bool


def nearby_assets(gateway: ImmichGateway, account: ImmichAccount, point: tuple[float, float], *, limit: int = NEARBY_ASSET_LIMIT) -> Neighbourhood:
    """The library's geolocated assets nearest ``point``, closest first.

    Args:
        gateway: The account's gateway, used only on a cache miss.
        account: Whose library this is; scopes the cache entry.
        point: ``(latitude, longitude)`` of the pin being searched around.
        limit: How many of the nearest assets to keep.

    Returns:
        The nearest assets and whether the cap dropped any.

    Raises:
        GatewayRequestError: On a network error or non-2xx response, from the underlying fetch."""
    key = _cache_key(account, point)
    cached = cache.get(key)
    if cached is not None:
        return cached

    measured = [(_haversine_km(point, (marker.lat, marker.lon)) * 1000, marker) for marker in gateway.get_map_markers()]
    measured.sort(key=lambda row: row[0])
    neighbourhood = Neighbourhood(nearest=measured[:limit], truncated=len(measured) > limit)
    cache.set(key, neighbourhood, NEARBY_CACHE_SECONDS)
    return neighbourhood


def within_radius(neighbourhood: Neighbourhood, radius_m: float) -> list[MapMarker]:
    """The subset of ``neighbourhood`` inside ``radius_m``, still closest first.

    Args:
        neighbourhood: Output of :func:`nearby_assets`.
        radius_m: The selected radius, in metres.

    Returns:
        The markers within that distance.
    """
    return [marker for distance_m, marker in neighbourhood.nearest if distance_m <= radius_m]
