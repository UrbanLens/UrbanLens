"""Extensible place-name resolution for newly created locations."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import Protocol

import requests

from urbanlens.dashboard.services.apis.locations import places_resolution
from urbanlens.dashboard.services.apis.locations.google.geocoding import GoogleGeocodingGateway
from urbanlens.dashboard.services.locations.naming import is_meaningful_name
from urbanlens.dashboard.services.rate_limiter import RequestCancelledError
from urbanlens.dashboard.services.redact import redact_coordinate
from urbanlens.UrbanLens.settings.app import settings

logger = logging.getLogger(__name__)


class PlaceNameResolver(Protocol):
    """Resolve a human-friendly place name for coordinates."""

    def resolve(self, latitude: float, longitude: float) -> str | None: ...


@dataclass(frozen=True, slots=True)
class GooglePlacesNameResolver:
    """Resolve names from Google Places nearby search results."""

    radius: int = 50

    def resolve(self, latitude: float, longitude: float) -> str | None:
        redata_configured = bool(settings.redata_api_url and settings.redata_api_key)
        if not settings.google_unrestricted_api_key and not redata_configured:
            return None
        return places_resolution.resolve_name_from_nearby(latitude, longitude, self.radius, api_key=settings.google_unrestricted_api_key or "")


@dataclass(frozen=True, slots=True)
class GoogleGeocodingNameResolver:
    """Fallback resolver using Google Geocoding formatted addresses."""

    def resolve(self, latitude: float, longitude: float) -> str | None:
        try:
            return GoogleGeocodingGateway(api_key=settings.google_unrestricted_api_key).get_place_name(latitude, longitude)
        except (OSError, ValueError, requests.RequestException, RequestCancelledError) as exc:
            logger.debug("Google Geocoding name lookup failed for %s,%s: %s", redact_coordinate(latitude), redact_coordinate(longitude), exc)
            return None


@dataclass(frozen=True, slots=True)
class PlaceNameResolverChain:
    """Try resolvers in order so future fallback strategies can be added cleanly."""

    resolvers: tuple[PlaceNameResolver, ...] = (GooglePlacesNameResolver(), GoogleGeocodingNameResolver())

    def resolve(self, latitude: float, longitude: float) -> str | None:
        for resolver in self.resolvers:
            name = resolver.resolve(latitude, longitude)
            if is_meaningful_name(name):
                return name
        return None
