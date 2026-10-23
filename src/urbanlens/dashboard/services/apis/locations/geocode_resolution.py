"""Resolves forward-geocoding (address -> coordinates) to the first geocoder that can answer."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.services.core.gateway import ProviderUnavailableError
from urbanlens.dashboard.services.security.redact import redact_coordinate, redact_text

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReverseGeocode:
    """What a geocoder knows about the place at a coordinate; blank where it knows nothing."""

    name: str = ""
    display_name: str = ""
    place_type: str = ""
    house_number: str = ""
    street: str = ""
    locality: str = ""
    county: str = ""
    region: str = ""
    postal_code: str = ""
    country: str = ""


@dataclass(frozen=True, slots=True)
class StreetViewCoverage:
    """Whether street-level imagery exists near a point, and the panorama that would be drawn."""

    available: bool
    pano_id: str = ""
    capture_date: str = ""
    latitude: float | None = None
    longitude: float | None = None


@dataclass(frozen=True, slots=True)
class StreetViewPicture:
    """One street-level picture and the panorama it shows."""

    content: bytes
    pano_id: str = ""
    capture_date: str = ""
    latitude: float | None = None
    longitude: float | None = None


class GeocodeProvider(ABC):
    """
    One source of forward geocoding, and of the map-service lookups a geocoder's own account also answers.

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

    def reverse_geocode(self, latitude: float, longitude: float) -> ReverseGeocode | None:
        """
        The place at a coordinate, for a provider that answers this; None when it does not or found nothing.

        Raises:
            ProviderUnavailableError: The provider could not answer, so the question went unasked.
        """
        return None

    def street_geocode(self, address: str) -> tuple[float, float] | None:
        """
        Resolve an address to coordinates only when the match is at street level or finer, for a provider that answers this.

        Raises:
            ProviderUnavailableError: The provider could not answer, so the question went unasked.
        """
        return None

    def street_view_service_keys(self) -> tuple[str, ...]:
        """Rate-limiter service keys one :meth:`street_view_picture` spends."""
        return ()

    def satellite_service_keys(self) -> tuple[str, ...]:
        """Rate-limiter service keys one :meth:`satellite_picture` spends."""
        return ()

    def street_view_coverage(self, latitude: float, longitude: float) -> StreetViewCoverage | None:
        """
        Whether street-level imagery exists at a coordinate.

        Raises:
            NotImplementedError: The provider does not answer this, which :func:`ask_shielding_geocoder` reads as not asked.
            ProviderUnavailableError: The provider could not answer, so the question went unasked.
        """
        raise NotImplementedError

    def street_view_picture(self, latitude: float, longitude: float) -> StreetViewPicture | None:
        """
        The nearest street-level picture of a coordinate; None when there is none.

        Raises:
            NotImplementedError: The provider does not answer this, which :func:`ask_shielding_geocoder` reads as not asked.
            ProviderUnavailableError: The provider could not answer, so the question went unasked.
        """
        raise NotImplementedError

    def satellite_picture(self, latitude: float, longitude: float) -> bytes | None:
        """
        A satellite picture centred on a coordinate; None when there is none.

        Raises:
            NotImplementedError: The provider does not answer this, which :func:`ask_shielding_geocoder` reads as not asked.
            ProviderUnavailableError: The provider could not answer, so the question went unasked.
        """
        raise NotImplementedError


class NominatimGeocoder(GeocodeProvider):
    """Direct OpenStreetMap Nominatim, held to its shared budget."""

    key: ClassVar[str] = "nominatim"

    def available(self) -> bool:
        """Always: Nominatim needs no key."""
        return True

    def geocode(self, address: str) -> tuple[float | None, float | None]:
        """See :func:`nominatim_geocode`."""
        return nominatim_geocode(address)


def shielded() -> bool:
    """Whether a shielding geocoder is available, so map-service lookups go to it rather than to Google with this deployment's own key."""
    from urbanlens.dashboard.plugins.points import GEOCODERS

    return any(provider.shields_direct and provider.available() for provider in GEOCODERS)


def shielding_service_keys(keys: Callable[[GeocodeProvider], tuple[str, ...]]) -> tuple[str, ...]:
    """The service keys the first available shielding geocoder spends on a lookup; empty when there is none."""
    from urbanlens.dashboard.plugins.points import GEOCODERS

    provider = next((candidate for candidate in GEOCODERS if candidate.shields_direct and candidate.available()), None)
    return keys(provider) if provider is not None else ()


def ask_shielding_geocoder[T](question: Callable[[GeocodeProvider], T]) -> tuple[bool, T | None]:
    """
    Put a question to the first available geocoder that shields direct providers, if there is one.

    Args:
        question: Called with that provider; returns its answer.

    Returns:
        ``(True, answer)`` once it was asked, the answer being None when it found nothing; ``(False, None)`` when no
        shielding geocoder is available or it could not answer on production, where the caller may ask its own provider.

    Raises:
        ProviderUnavailableError: The shielding geocoder could not answer, off production, where the direct fallback is not asked.
    """
    from urbanlens.dashboard.plugins.points import GEOCODERS
    from urbanlens.dashboard.services.core.egress import direct_fallback_permitted

    provider = next((candidate for candidate in GEOCODERS if candidate.shields_direct and candidate.available()), None)
    if provider is None:
        return False, None
    try:
        return True, question(provider)
    except NotImplementedError:
        return False, None
    except ProviderUnavailableError as exc:
        if not direct_fallback_permitted():
            logger.info("%s geocode unavailable (%s); not falling back to a direct provider off production", provider.key, exc.reason)
            raise
        logger.warning("%s geocode failed, asking the direct provider: %s", provider.key, exc.reason)
        return False, None


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
