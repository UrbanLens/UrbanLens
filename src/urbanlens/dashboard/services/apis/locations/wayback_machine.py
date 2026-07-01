"""Internet Archive Wayback Machine gateway for archived web resources."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from requests import Response

from urbanlens.dashboard.services.gateway import Gateway

_AVAILABILITY_URL = "https://archive.org/wayback/available"
_CDX_URL = "https://web.archive.org/cdx"
_SAVE_URL = "https://web.archive.org/save"
_MEMENTO_TIMEMAP_URL = "https://web.archive.org/web/timemap/json"


@dataclass(frozen=True, slots=True, kw_only=True)
class WaybackMachineGateway(Gateway):
    """Gateway for Internet Archive Wayback Machine APIs."""

    service_key: ClassVar[str] = "wayback_machine"

    def get_availability(self, url: str, *, timestamp: str | None = None) -> dict[str, Any]:
        """Return the closest archived snapshot for a URL."""
        params = {"url": url}
        if timestamp:
            params["timestamp"] = timestamp
        response = self.session.get(_AVAILABILITY_URL, params=params, timeout=10)
        response.raise_for_status()
        return response.json()

    def search_cdx(self, url: str, **params: Any) -> list[Any]:
        """Search the CDX index for captures of a URL or URL pattern."""
        query = {"url": url, "output": "json", **params}
        response = self.session.get(_CDX_URL, params=query, timeout=20)
        response.raise_for_status()
        return response.json()

    def get_memento_timemap(self, url: str, **params: Any) -> list[Any]:
        """Return a Memento TimeMap listing archived captures for a URL."""
        response = self.session.get(_MEMENTO_TIMEMAP_URL, params={"url": url, **params}, timeout=20)
        response.raise_for_status()
        return response.json()

    def save_url(self, url: str, **params: Any) -> Response:
        """Ask Wayback Machine to archive a URL now and return the raw response."""
        return self.session.get(f"{_SAVE_URL}/{url}", params=params, timeout=30)
