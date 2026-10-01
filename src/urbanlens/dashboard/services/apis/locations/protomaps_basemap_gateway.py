"""Protomaps' hosted basemap, fetched by this origin rather than by every browser.

Their CDN publishes a tile as ``cache-control: public, max-age=14400`` and then answers with an
``age`` well past it - measured against ``k3s-staging`` on 2026-09-22, every tile of a viewport
arrived ``age=52283`` against that 4-hour lifetime. A response that is already stale cannot be
reused without revalidating, and a revalidation is another billed request, so a browser pointed at
this API pays for the same tile every time it draws it. Fetching here puts this deployment's cache
in front of the meter instead, where one fetch answers every viewer.

The style documents come through here for the same reason and one more: upstream sends the 65kB
document with no cache headers at all, and it carries the API key.
"""

from __future__ import annotations

from typing import ClassVar
from urllib.parse import quote, urlparse

from urbanlens.dashboard.services.core.gateway import Gateway, read_capped

#: Sent because a bare requests agent is what a CDN refuses first. The fetch is server-side, so no
#: viewer's Referer or coordinates travel with it.
_USER_AGENT = "UrbanLens/1.0 (+https://urbanlens.org; basemap tile proxy)"

#: The themes Protomaps publishes that this deployment offers. Their set is larger; these are the
#: two REData's ``street`` and ``dark`` layers map onto.
SERVED_THEMES = frozenset({"light", "dark"})

_API_ROOT = "https://api.protomaps.com"


class ProtomapsBasemapGateway(Gateway):
    """Fetches one vector tile, or one style document, from Protomaps' hosted API.

    Every call needs an ``Origin``. Measured against the live key on 2026-09-22, a request carrying
    none is refused with 403 and a request carrying *any* is answered with 200 - including an origin
    nobody listed on the key. So the header is what makes a server-side fetch possible, and the key
    is not in practice restricted to the sites its owner named: a copy lifted from a page spends
    this deployment's quota from anywhere. Keeping it server-side is therefore worth doing on its
    own, separately from the caching this class exists for.
    """

    service_key: ClassVar[str] = "protomaps_basemap"

    @staticmethod
    def endpoint_for_log(url: str) -> str:
        """Record which endpoint was called, never the coordinate and never the key.

        Which tiles a viewer asked for is which places they were looking at, so a coordinate must
        no more reach ``ApiCallLog`` than it reaches the vendor's logs with a Referer.

        Args:
            url: The URL about to be requested.

        Returns:
            The URL truncated before the coordinate, with no query string.
        """
        parsed = urlparse(url)
        if parsed.path.startswith("/tiles/"):
            return f"{parsed.scheme}://{parsed.netloc}/tiles/v4/"
        return f"{parsed.scheme}://{parsed.netloc}/styles/v5/"

    def _get(self, url: str, origin: str, what: str, default_type: str) -> tuple[int, bytes, str]:
        """Perform one authorised fetch.

        Args:
            url: The upstream URL, key included.
            origin: The ``Origin`` to present; without one the API answers 403.
            what: Label for the size cap's error message.
            default_type: Content type to assume when the upstream declares none.

        Returns:
            ``(status_code, body, content_type)``.
        """
        response = self.session.get(
            url,
            headers={"User-Agent": _USER_AGENT, "Origin": origin, "Accept": "*/*"},
            timeout=30,
            stream=True,
        )
        body = read_capped(response, what=what)
        return response.status_code, body, response.headers.get("Content-Type", default_type)

    def download_tile(self, z: int, x: int, y: int, *, key: str, origin: str) -> tuple[int, bytes, str]:
        """Fetch one vector tile.

        Args:
            z: Tile zoom level.
            x: Tile column.
            y: Tile row.
            key: This deployment's Protomaps API key.
            origin: The ``Origin`` to present.

        Returns:
            ``(status_code, body, content_type)``.
        """
        url = f"{_API_ROOT}/tiles/v4/{z}/{x}/{y}.mvt?key={quote(key, safe='')}"
        return self._get(url, origin, what="vector basemap tile", default_type="application/x-protobuf")

    def download_style(self, theme: str, *, key: str, origin: str) -> tuple[int, bytes, str]:
        """Fetch one style document.

        Args:
            theme: A member of :data:`SERVED_THEMES`.
            key: This deployment's Protomaps API key.
            origin: The ``Origin`` to present.

        Returns:
            ``(status_code, body, content_type)``.
        """
        url = f"{_API_ROOT}/styles/v5/{quote(theme, safe='')}/en.json?key={quote(key, safe='')}"
        return self._get(url, origin, what="vector basemap style", default_type="application/json")
