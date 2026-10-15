"""Resolves route-between-waypoints calls to the first routing provider that can answer, per call."""

from __future__ import annotations

from abc import ABC, abstractmethod
import logging
from typing import Any, ClassVar

from urbanlens.dashboard.services.core.gateway import ProviderUnavailableError

logger = logging.getLogger(__name__)


class RouteProvider(ABC):
    """One source of driving routes between two points."""

    key: ClassVar[str]

    @abstractmethod
    def available(self) -> bool:
        """Whether this provider is configured to answer."""

    @abstractmethod
    def get_route(self, origin: tuple[float, float], destination: tuple[float, float]) -> dict[str, Any] | None:
        """
        Route between two points.

        Args:
            origin: ``(latitude, longitude)`` of the starting point.
            destination: ``(latitude, longitude)`` of the destination.

        Returns:
            ``{"distance_meters", "duration_seconds"}``, or None when no route connects the points.

        Raises:
            ProviderUnavailableError: The provider could not answer, so the next may be tried.
        """


class OsrmRouteProvider(RouteProvider):
    """Direct OSRM."""

    key: ClassVar[str] = "osrm"

    def available(self) -> bool:
        """Always: OSRM needs no key."""
        return True

    def get_route(self, origin: tuple[float, float], destination: tuple[float, float]) -> dict[str, Any] | None:
        """See :meth:`OSRMGateway.get_route_between`."""
        from urbanlens.dashboard.services.apis.routing.osrm import OSRMGateway

        return OSRMGateway().get_route_between(origin, destination)


def get_route_between(origin: tuple[float, float], destination: tuple[float, float]) -> dict[str, Any] | None:
    """
    Route between two points, trying each available provider in priority order.

    Args:
        origin: ``(latitude, longitude)`` of the starting point.
        destination: ``(latitude, longitude)`` of the destination.

    Returns:
        ``{"distance_meters", "duration_seconds"}``, or None when no route connects the points (or every provider failed)."""
    from urbanlens.dashboard.plugins.points import ROUTE_PROVIDERS

    for provider in ROUTE_PROVIDERS:
        if not provider.available():
            continue
        try:
            return provider.get_route(origin, destination)
        except ProviderUnavailableError as exc:
            logger.warning("%s routing failed, trying the next provider: %s", provider.key, exc)
    return None
