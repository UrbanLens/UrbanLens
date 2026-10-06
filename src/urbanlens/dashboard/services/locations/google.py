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
    """Resolve names from Google Places nearby search: REData's cache of it when REData is configured."""

    radius: int = 50

    @property
    def direct(self) -> bool:
        """Direct Google Places only when REData is not configured."""
        return not _redata_configured()

    def resolve(self, latitude: float, longitude: float) -> str | None:
        if not settings.google_unrestricted_api_key and not _redata_configured():
            return None
        return places_resolution.resolve_name_from_nearby(latitude, longitude, self.radius, api_key=settings.google_unrestricted_api_key or "")


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
    """Try resolvers in order so fallback strategies can be added.

    Off production, once REData has been asked, the chain stops before any resolver that asks a ``quota`` or
    ``billed`` provider directly (D26): a REData that failed or found nothing is reported as no name for now,
    which the caller retries later, rather than pushing its load onto direct Google from an address
    production shares.
    """

    resolvers: tuple[PlaceNameResolver, ...] = (GooglePlacesNameResolver(), GoogleGeocodingNameResolver())

    def resolve(self, latitude: float, longitude: float) -> str | None:
        from urbanlens.dashboard.services.core.egress import direct_fallback_permitted

        asked_redata = False
        for resolver in self.resolvers:
            direct = bool(getattr(resolver, "direct", False))
            if direct and asked_redata and not direct_fallback_permitted():
                logger.info("Place name for %s,%s unavailable from REData; not falling through to a direct provider off production", redact_coordinate(latitude), redact_coordinate(longitude))
                return None
            asked_redata = asked_redata or not direct
            name = resolver.resolve(latitude, longitude)
            if is_meaningful_name(name):
                return name
        return None


def _redata_configured() -> bool:
    return bool(settings.redata_api_url and settings.redata_api_key)
