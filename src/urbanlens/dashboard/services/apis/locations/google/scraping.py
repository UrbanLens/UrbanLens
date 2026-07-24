"""Proof-of-concept: resolve a Google Maps place's coordinates via headless browser.

This is a standalone alternative to :class:`GoogleGeocodingGateway`'s
``get_coordinates_by_cid`` - instead of paying for the Places Details API (or
trusting the unreliable literal-S2-decode heuristic), it drives a real headless
browser to the CID redirect URL and reads back the coordinates Google's own
client-side JS resolves the place to.

Not wired into the import pipeline. This is a pathway to prove out and expand on,
not a replacement for the existing geocoding service. See
notes/geocoding-analysis/ for the accuracy validation run against real,
API-verified ground truth.
"""

from __future__ import annotations

import logging
import re
from types import TracebackType
from typing import Self

from playwright.sync_api import Browser, Playwright, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

logger = logging.getLogger(__name__)

# The `!3d{lat}!4d{lon}` pair in a resolved Maps URL's data segment is the place's
# own coordinate. The `/@{lat},{lon}` pair is the viewport center, which Google
# usually centers on the place too, but it's a weaker fallback (map may have
# settled on a rounder/panned position).
_COORD_DATA_RE = re.compile(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)")
_COORD_URL_RE = re.compile(r"/@(-?\d+\.\d+),(-?\d+\.\d+)")
_RESOLVED_URL_RE = re.compile(r"/@-?\d+\.\d+,-?\d+\.\d+")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class GoogleMapsScraper:
    """Resolves Google Maps CIDs/URLs to coordinates via a real headless browser.

    Usage::

        with GoogleMapsScraper() as scraper:
            lat, lon = scraper.resolve_by_cid(6952009488037205194)

    Reuses one browser instance across many lookups within the `with` block -
    each call pays only the cost of a page navigation (roughly 1-3s), not a
    fresh browser launch.
    """

    def __init__(self, *, headless: bool = True, timeout_ms: int = 20000) -> None:
        self.timeout_ms = timeout_ms
        self._headless = headless
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    def __enter__(self) -> Self:
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self._headless)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None

    def resolve_by_cid(self, cid: int) -> tuple[float | None, float | None]:
        """Resolve coordinates for a Google Maps CID (the ``0x...`` value after the ``:`` in a place URL's data segment)."""
        return self.resolve_from_url(f"https://www.google.com/maps?cid={cid}")

    def resolve_from_url(self, url: str) -> tuple[float | None, float | None]:
        """Navigate to a Google Maps URL and read back the coordinates it resolves to."""
        if self._browser is None:
            raise RuntimeError("GoogleMapsScraper must be used as a context manager (`with GoogleMapsScraper() as scraper:`)")

        page = self._browser.new_page(user_agent=_USER_AGENT)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            try:
                page.wait_for_url(_RESOLVED_URL_RE, timeout=self.timeout_ms)
            except PlaywrightTimeoutError:
                logger.warning("Timed out waiting for Google Maps to resolve coordinates for %s", url)

            final_url = page.url
            match = _COORD_DATA_RE.search(final_url) or _COORD_URL_RE.search(final_url)
            if not match:
                logger.warning("Could not extract coordinates from resolved Maps URL: %s", final_url)
                return None, None
            return float(match.group(1)), float(match.group(2))
        finally:
            page.close()
