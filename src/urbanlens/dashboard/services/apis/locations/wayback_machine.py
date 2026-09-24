"""Internet Archive Wayback Machine gateway for archived web resources."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar
from urllib.parse import urlparse

import requests

from urbanlens.dashboard.services.core.gateway import Gateway
from urbanlens.dashboard.services.security.url_safety import UnsafeUrlError, open_public_url

_AVAILABILITY_URL = "https://archive.org/wayback/available"
_CDX_URL = "https://web.archive.org/cdx/search/cdx"
_SAVE_URL = "https://web.archive.org/save"
#: The save path embeds the user's link, so the whole url can outgrow the default ceiling.
_MAX_SAVE_URL_LENGTH = 4096
#: A save makes the Archive fetch the page first, so it is slower than a lookup.
_SAVE_DEADLINE_SECONDS = 90
#: A save may redirect to its snapshot; anywhere else would be this server fetching a url a user chose.
_ARCHIVE_HOSTS = (".archive.org",)
_MEMENTO_TIMEMAP_URL = "https://web.archive.org/web/timemap"

# Always excluded regardless of this deployment's own SITE_URL, so a
# self-hosted instance (running under a different domain) still won't submit
# the canonical site's own URLs either.
_ALWAYS_EXCLUDED_DOMAINS = ("urbanlens.org",)


def is_own_site_url(url: str) -> bool:
    """True when ``url`` points at this deployment's own domain (or urbanlens.org).
    Most pages on this site require being logged in, so archiving them on the Wayback Machine wouldn't produce anything a future anonymous visitor could actually read - checked before submitting any link a user adds for a pin/wiki.

    Args:
        url: The URL a user is asking to be archived.

    Returns:
        True if the URL's host is this site's own domain or urbanlens.org (or a subdomain of either), meaning it should not be submitted."""
    from django.conf import settings

    hostname = (urlparse(url).hostname or "").lower().rstrip(".")
    if not hostname:
        return False
    site_hostname = (urlparse(settings.SITE_URL).hostname or "").lower()
    excluded_domains = {domain for domain in (site_hostname, *_ALWAYS_EXCLUDED_DOMAINS) if domain}
    return any(hostname == domain or hostname.endswith(f".{domain}") for domain in excluded_domains)


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
            Parsed JSON with an ``archived_snapshots`` dict, which may be empty if the URL has never been archived.
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
            List of capture rows.
        """
        query = {"url": url, "output": "json", **params}
        response = self.session.get(_CDX_URL, params=query, timeout=20)
        response.raise_for_status()
        return response.json()

    def get_memento_timemap(self, url: str, *, output: str = "json", **params: Any) -> list[Any] | str:
        """Return a Memento TimeMap listing archived captures for a URL.

        Args:
            url: The URL to retrieve the TimeMap for.
            output: Response format - ``"json"`` (default), ``"link"``, or ``"cdxj"``.
            **params: Additional query parameters.

        Returns:
            Parsed list of Memento records when ``output="json"``, otherwise the raw text body.
        """
        timemap_url = f"{_MEMENTO_TIMEMAP_URL}/{output}/{url}"
        response = self.session.get(timemap_url, params=params, timeout=20)
        response.raise_for_status()
        if output == "json":
            return response.json()
        return response.text

    def save_url(self, url: str, **params: Any) -> dict[str, Any]:
        """Ask the Wayback Machine to archive a URL now.

        Args:
            url: The URL to archive.
            **params: Additional query parameters passed to the save endpoint.

        Returns:
            Dict with ``"archived_url"`` (the saved copy's URL) and ``"status_code"`` (HTTP status of the final response).

        Raises:
            requests.RequestException: The save failed, outlasted its deadline, or redirected off archive.org.
        """
        try:
            with open_public_url(
                "GET",
                f"{_SAVE_URL}/{url}",
                session=self.session,
                params=params or None,
                timeout=30,
                total_deadline=_SAVE_DEADLINE_SECONDS,
                allowed_redirect_hosts=_ARCHIVE_HOSTS,
                max_length=_MAX_SAVE_URL_LENGTH,
            ) as response:
                response.raise_for_status()
                return {"archived_url": response.url, "status_code": response.status_code}
        except UnsafeUrlError as exc:
            raise requests.RequestException(f"The Wayback save was refused: {exc}") from exc
