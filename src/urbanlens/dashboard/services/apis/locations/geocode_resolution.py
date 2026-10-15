"""Resolves forward-geocoding (address -> coordinates) to the first geocoder that can answer."""

from __future__ import annotations

from abc import ABC, abstractmethod
import logging
from typing import ClassVar

from urbanlens.dashboard.services.core.gateway import ProviderUnavailableError
from urbanlens.dashboard.services.security.redact import redact_coordinate, redact_text

logger = logging.getLogger(__name__)


class GeocodeProvider(ABC):
    """
    One source of forward geocoding.

    Attributes:
        key: Short identifier, for logs.
        shields_direct: Whether, off production, this provider failing should end the chain rather than fall through to
            a provider that spends this deployment's own budget: a busy first-party service must not push its
            load onto a public API from an address production shares.
    """

    key: ClassVar[str]
    shields_direct: ClassVar[bool] = False

    @abstractmethod
    def available(self) -> bool:
        """Whether this provider is configured to answer."""

    @abstractmethod
    def geocode(self, address: str) -> tuple[float | None, float | None]:
        """
        Resolve one address.

        Args:
            address: The address string to geocode.

        Returns:
            ``(latitude, longitude)``, or ``(None, None)`` when the address resolves to no place.

        Raises:
            ProviderUnavailableError: The provider could not answer, so the question went unasked.
        """


class NominatimGeocoder(GeocodeProvider):
    """Direct OpenStreetMap Nominatim, held to its shared budget."""

    key: ClassVar[str] = "nominatim"

    def available(self) -> bool:
        """Always: Nominatim needs no key."""
        return True

    def geocode(self, address: str) -> tuple[float | None, float | None]:
        """See :func:`nominatim_geocode`."""
        return nominatim_geocode(address)


def geocode_address(address: str) -> tuple[float | None, float | None]:
    """
    Resolve a free-text address to coordinates, asking each available geocoder in priority order.

    Args:
        address: The address string to geocode.

    Returns:
        A ``(latitude, longitude)`` tuple, or ``(None, None)`` when the address doesn't resolve to a place anywhere.

    Raises:
        ProviderUnavailableError: A shielding provider could not answer, off production, where the direct fallback is not
            asked.
        RateLimitExceededError: The app-wide Nominatim budget refused the fallback call (see :func:`nominatim_geocode`).
        EnvironmentRefusedError: This environment does not call Nominatim (see :func:`nominatim_geocode`)."""
    from urbanlens.dashboard.plugins.points import GEOCODERS
    from urbanlens.dashboard.services.core.egress import direct_fallback_permitted

    for provider in GEOCODERS:
        if not provider.available():
            continue
        try:
            return provider.geocode(address)
        except ProviderUnavailableError as exc:
            if provider.shields_direct and not direct_fallback_permitted():
                logger.info("%s geocode unavailable for an address lookup (%s); not falling back to a direct provider off production", provider.key, exc.reason)
                raise
            logger.warning("%s geocode failed for an address lookup, trying the next provider: %s", provider.key, exc.reason)
    return (None, None)


def nominatim_geocode(address: str) -> tuple[float | None, float | None]:
    """
    Forward-geocode one address through the project's Nominatim gateway.

    Args:
        address: The address string to geocode.

    Returns:
        A ``(latitude, longitude)`` tuple, or ``(None, None)`` when Nominatim has no such place or the request failed (the gateway flattens failures to an empty result).

    Raises:
        RateLimitExceededError: The app-wide Nominatim budget refused the call - propagated so a caller cannot mistake "we did not ask" for "no such place".
        EnvironmentRefusedError: This environment does not call Nominatim, propagated for the same reason."""
    from urbanlens.dashboard.services.apis.locations.nominatim import NominatimGateway
    from urbanlens.dashboard.services.core.input_validation import ImpossibleInputError

    try:
        results = NominatimGateway().search(address, limit=1)
    except ImpossibleInputError:
        return (None, None)
    if results:
        first = results[0]
        latitude, longitude = first.get("lat"), first.get("lon")
        if latitude is not None and longitude is not None:
            try:
                return float(latitude), float(longitude)
            except (TypeError, ValueError):
                logger.warning(
                    "Nominatim returned unparseable coordinates for %s: %s, %s",
                    redact_text(address),
                    redact_coordinate(latitude),
                    redact_coordinate(longitude),
                )
    return (None, None)
