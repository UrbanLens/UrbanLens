"""SearXNG metasearch gateway.

SearXNG (https://docs.searxng.org/) is an open-source, self-hostable
metasearch engine that aggregates results from many upstream engines
(Google, Bing, DuckDuckGo, Brave, and dozens more) behind one privacy-respecting
API. There is no central SearXNG API key - each instance is independent, so
this gateway talks to whichever instance the admin configures.

Most public instances disable the JSON output format to discourage scraping,
so this integration is intended for a self-hosted instance (trivial to run
via the official Docker image) or a instance the admin has explicit
permission to query programmatically. No default base URL is supplied.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any, ClassVar

from requests import HTTPError

from urbanlens.dashboard.services.gateway import Gateway
from urbanlens.UrbanLens.settings.app import settings

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

#: SearXNG image engines queried by :meth:`SearxngGateway.search_images` when
#: the admin hasn't overridden the list via ``UL_SEARXNG_IMAGE_ENGINES``. These
#: are the engine *names* as configured in the instance's ``settings.yml`` (the
#: SearXNG ``engines`` request parameter expects those names, comma-joined) -
#: they're image-only, freely-licensed, or archival sources appropriate to the
#: site's subject matter. An engine an instance hasn't enabled is silently
#: ignored by SearXNG, so an over-broad default is harmless.
DEFAULT_IMAGE_ENGINES: tuple[str, ...] = (
    "google cse images",
    "startpage images",
    "artic",
    "deviantart",
    "flickr",
    "imgur",
    "library of congress",
    "openverse",
    "pexels",
    "pinterest",
    "public domain image archive",
    "pixabay images",
    "unsplash",
    "wikicommons.images",
)


class SearxngError(RuntimeError):
    """Raised when a SearXNG instance cannot complete a search request."""


@dataclass(slots=True, kw_only=True)
class SearxngGateway(Gateway):
    """Gateway for a self-hosted or trusted SearXNG metasearch instance.

    Docs: https://docs.searxng.org/dev/search_api.html
    Auth: none - configure ``base_url`` to point at an instance with JSON
    output enabled (``search.formats: [html, json]`` in ``settings.yml``).
    """

    service_key: ClassVar[str] = "searxng"
    paid_service: ClassVar[bool] = False

    base_url: str | None = None

    def __post_init__(self) -> None:
        Gateway.__post_init__(self)
        if self.base_url is None:
            object.__setattr__(self, "base_url", settings.searxng_base_url)

    def search(self, query: str, *, max_results: int = 10) -> list[dict[str, Any]]:
        """Perform a SearXNG search and return normalised result dicts.

        Args:
            query: The search string.
            max_results: Maximum number of results to return.

        Returns:
            List of dicts with keys ``title``, ``link``, ``snippet``.

        Raises:
            SearxngError: When no instance is configured or the request fails.
        """
        self._validate()
        params = {"q": query, "format": "json"}
        response = self.session.get(f"{self.base_url}/search", params=params, timeout=60)
        try:
            response.raise_for_status()
        except HTTPError as exc:
            logger.warning("SearXNG request to %s failed with status %s", self.base_url, response.status_code)
            raise SearxngError(f"SearXNG request failed with status {response.status_code}") from exc
        return self._parse(response.json())[:max_results]

    def search_images(self, query: str, *, engines: Sequence[str] | None = None, max_results: int = 30) -> list[dict[str, Any]]:
        """Perform a SearXNG image search and return normalised image dicts.

        Restricts SearXNG to its ``images`` category and to a curated set of
        image/archive engines (see :data:`DEFAULT_IMAGE_ENGINES`), so the
        aggressive relevance query the caller builds is only ever matched
        against image results rather than the general web.

        Args:
            query: The search string (typically the grouped relevance query
                built by the SearXNG-images media provider).
            engines: Engine names to query; defaults to
                ``settings.searxng_image_engines`` when set, otherwise
                :data:`DEFAULT_IMAGE_ENGINES`.
            max_results: Maximum number of images to return.

        Returns:
            List of dicts with keys ``url`` (full image), ``thumbnail``,
            ``title``, ``page_url`` (the page the image was found on), and
            ``source`` (the upstream engine/site).

        Raises:
            SearxngError: When no instance is configured or the request fails.
        """
        self._validate()
        engine_names = list(engines) if engines is not None else (settings.searxng_image_engines or DEFAULT_IMAGE_ENGINES)
        params = {"q": query, "format": "json", "categories": "images", "engines": ",".join(engine_names)}
        response = self.session.get(f"{self.base_url}/search", params=params, timeout=60)
        try:
            response.raise_for_status()
        except HTTPError as exc:
            logger.warning("SearXNG image request to %s failed with status %s", self.base_url, response.status_code)
            raise SearxngError(f"SearXNG image request failed with status {response.status_code}") from exc
        return self._parse_images(response.json())[:max_results]

    def _validate(self) -> None:
        if not self.base_url:
            raise SearxngError("UL_SEARXNG_BASE_URL is not configured. Point it at a self-hosted or trusted SearXNG instance with JSON output enabled.")

    def _absolute(self, url: str | None) -> str:
        """Resolve a possibly instance-relative image URL to an absolute one.

        SearXNG proxies some thumbnails through its own ``/image_proxy`` path
        and returns them as instance-relative URLs; those only resolve against
        the instance's base URL, so prefix them here.

        Args:
            url: A raw ``img_src``/``thumbnail_src`` value, or None.

        Returns:
            An absolute URL, or ``""`` when ``url`` is falsy.
        """
        if not url:
            return ""
        if url.startswith("/") and self.base_url:
            return f"{self.base_url.rstrip('/')}{url}"
        return url

    def _parse_images(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for item in data.get("results", []):
            image_url = self._absolute(item.get("img_src"))
            if not image_url:
                # Image-category results without a usable image (rare) are just
                # noise in a media gallery - skip them rather than render a
                # broken tile.
                continue
            results.append(
                {
                    "url": image_url,
                    "thumbnail": self._absolute(item.get("thumbnail_src")) or image_url,
                    "title": item.get("title") or "",
                    "page_url": item.get("url") or "",
                    "source": item.get("source") or item.get("engine") or "",
                },
            )
        return results

    def _parse(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for item in data.get("results", []):
            results.append(
                {
                    "title": item.get("title"),
                    "link": item.get("url"),
                    "snippet": item.get("content"),
                    "date": item.get("publishedDate"),
                    "thumbnail": item.get("img_src"),
                },
            )
        return results
