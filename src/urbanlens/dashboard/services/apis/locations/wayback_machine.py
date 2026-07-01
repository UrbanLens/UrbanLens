"""Internet Archive Wayback Machine gateway for archived web resources."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from urbanlens.dashboard.services.gateway import Gateway

_AVAILABILITY_URL = "https://archive.org/wayback/available"
_CDX_URL = "https://web.archive.org/cdx/search/cdx"
_SAVE_URL = "https://web.archive.org/save"
_MEMENTO_TIMEMAP_URL = "https://web.archive.org/web/timemap"


@dataclass(slots=True, kw_only=True)
class WaybackMachineGateway(Gateway):
    """Gateway for Internet Archive Wayback Machine APIs."""

    service_key: ClassVar[str] = "wayback_machine"
    paid_service: ClassVar[bool] = False

    def get_availability(self, url: str, *, timestamp: str | None = None) -> dict[str, Any]:
        """Return the closest archived snapshot for a URL.

        Args:
            url: The URL to look up.
            timestamp: Optional 14-digit timestamp (YYYYMMDDhhmmss) to find the
                nearest capture to a specific point in time.

        Returns:
            Parsed JSON with an ``archived_snapshots`` dict, which may be empty
            if the URL has never been archived.
        """
        params: dict[str, Any] = {"url": url}
        if timestamp:
            params["timestamp"] = timestamp
        response = self.session.get(_AVAILABILITY_URL, params=params, timeout=10)
        response.raise_for_status()
        return response.json()

    def search_cdx(self, url: str, **params: Any) -> list[Any]:
        """Search the CDX index for captures of a URL or URL pattern.

        Args:
            url: URL or URL prefix/pattern to search for.  Supports ``*`` wildcards
                when ``matchType`` is set to ``"prefix"`` or ``"domain"``.
            **params: Additional CDX API parameters (e.g. ``from_``, ``to``,
                ``limit``, ``matchType``, ``filter``, ``fl``).

        Returns:
            List of capture rows.  With ``output="json"`` (the default), the first
            row is the field header and subsequent rows are captures.
        """
        query = {"url": url, "output": "json", **params}
        response = self.session.get(_CDX_URL, params=query, timeout=20)
        response.raise_for_status()
        return response.json()

    def get_memento_timemap(self, url: str, *, output: str = "json", **params: Any) -> list[Any] | str:
        """Return a Memento TimeMap listing archived captures for a URL.

        Args:
            url: The URL to retrieve the TimeMap for.
            output: Response format — ``"json"`` (default), ``"link"``, or ``"cdxj"``.
            **params: Additional query parameters.

        Returns:
            Parsed list of Memento records when ``output="json"``, otherwise the
            raw text body.
        """
        timemap_url = f"{_MEMENTO_TIMEMAP_URL}/{output}/{url}"
        response = self.session.get(timemap_url, params=params, timeout=20)
        response.raise_for_status()
        if output == "json":
            return response.json()
        return response.text

    def save_url(self, url: str, **params: Any) -> dict[str, Any]:
        """Ask the Wayback Machine to archive a URL now.

        The save API redirects to the archived copy on success; this method
        follows the redirect and returns the final location.

        Args:
            url: The URL to archive.
            **params: Additional query parameters passed to the save endpoint.

        Returns:
            Dict with ``"archived_url"`` (the saved copy's URL) and
            ``"status_code"`` (HTTP status of the final response).
        """
        response = self.session.get(f"{_SAVE_URL}/{url}", params=params, timeout=30, allow_redirects=True)
        response.raise_for_status()
        return {"archived_url": response.url, "status_code": response.status_code}
