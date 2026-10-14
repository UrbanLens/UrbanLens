"""Extensible place-name resolution for newly created locations."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import ClassVar, Protocol

import requests

from urbanlens.dashboard.services.apis.locations import places_resolution
from urbanlens.dashboard.services.apis.locations.google.geocoding import GoogleGeocodingGateway
from urbanlens.dashboard.services.core.rate_limiter import RequestCancelledError
from urbanlens.dashboard.services.locations.naming import is_meaningful_name
from urbanlens.dashboard.services.security.redact import redact_coordinate
from urbanlens.UrbanLens.settings.app import settings

logger = logging.getLogger(__name__)


class PlaceNameResolver(Protocol):
    """Resolve a human-friendly place name for coordinates."""

    def resolve(self, latitude: float, longitude: float) -> str | None: ...


@dataclass(frozen=True, slots=True)
class GooglePlacesNameResolver:
    """Resolve names from nearby places search, through whichever places provider is available."""

    radius: int = 50

    @property
    def direct(self) -> bool:
        """Whether the active places provider spends this deployment's own budget rather than shielding it."""
        return not places_resolution.shields_direct()

    def resolve(self, latitude: float, longitude: float) -> str | None:
        return places_resolution.resolve_name_from_nearby(latitude, longitude, self.radius)


@dataclass(frozen=True, slots=True)
class GoogleGeocodingNameResolver:
    """Fallback resolver using Google Geocoding addresses, asked directly."""

    direct: ClassVar[bool] = True

    def resolve(self, latitude: float, longitude: float) -> str | None:
        try:
            return GoogleGeocodingGateway(api_key=settings.google_unrestricted_api_key).get_place_name(latitude, longitude)
        except (OSError, ValueError, requests.RequestException, RequestCancelledError) as exc:
            logger.debug("Google Geocoding name lookup failed for %s,%s: %s", redact_coordinate(latitude), redact_coordinate(longitude), exc)
            return None


@dataclass(frozen=True, slots=True)
class PlaceNameResolverChain:
    """
    Try resolvers in order so fallback strategies can be added.

    Off production, once a shielding provider has been asked, the chain stops before any resolver that asks a ``quota`` or
    ``billed`` provider directly: a provider that failed or found nothing is reported as no name for now,
    which the caller retries later, rather than pushing its load onto direct Google from an address
    production shares.
    """

    resolvers: tuple[PlaceNameResolver, ...] = (GooglePlacesNameResolver(), GoogleGeocodingNameResolver())

    def resolve(self, latitude: float, longitude: float) -> str | None:
        from urbanlens.dashboard.services.core.egress import direct_fallback_permitted

        asked_shield = False
        for resolver in self.resolvers:
            direct = bool(getattr(resolver, "direct", False))
            if direct and asked_shield and not direct_fallback_permitted():
                logger.info("Place name for %s,%s unavailable from the shielding provider; not falling through to a direct provider off production", redact_coordinate(latitude), redact_coordinate(longitude))
                return None
            asked_shield = asked_shield or not direct
            name = resolver.resolve(latitude, longitude)
            if is_meaningful_name(name):
                return name
        return None
