"""LoopNet service — best-effort commercial real-estate data for a location."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import Any, ClassVar
from urllib.parse import quote_plus

from urbanlens.dashboard.services.gateway import Gateway

logger = logging.getLogger(__name__)

# LoopNet's bot-detection is strong; we use the web-search fallback by default.
_LOOPNET_SEARCH_TMPL = "https://www.loopnet.com/search/commercial-real-estate/{address}/for-lease-and-sale/"
_LOOPNET_SITE_SEARCH = 'site:loopnet.com "{address}"'

# Brave / Google search is attempted first via the existing search gateways.
_MAX_SEARCH_RESULTS = 5


@dataclass(slots=True, kw_only=True)
class LoopNetGateway(Gateway):
    """
    Retrieves LoopNet commercial real-estate listing data for an address.

    Strategy:
    1. Attempt a direct LoopNet search page fetch (often blocked by Cloudflare).
    2. Fall back to a web-search query (``site:loopnet.com "address"``) which
       returns snippets that can still surface useful property metadata.

    Both paths cache results via the LocationCache layer in the controller.
    """

    service_key: ClassVar[str] = "loopnet"
    paid_service: ClassVar[bool] = False

    base_url: str = "https://www.loopnet.com"

    def search(self, address: str) -> dict[str, Any] | None:
        """
        Search LoopNet for *address* and return structured property data.

        Args:
            address: Full street address string, e.g. "123 Main St, Albany, NY".

        Returns:
            Dict with keys ``listings`` (list of dicts) and ``search_url``, or
            None if nothing was found or all attempts were blocked.
        """
        direct = self._direct_search(address)
        if direct:
            return direct

        return self._web_search_fallback(address)

    # ── private ────────────────────────────────────────────────────────────────

    def _direct_search(self, address: str) -> dict[str, Any] | None:
        """Try fetching the LoopNet search results page directly."""
        import requests.exceptions

        slug = quote_plus(address.replace(",", "").replace("  ", " "))
        url = _LOOPNET_SEARCH_TMPL.format(address=slug)
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }
        try:
            resp = self.session.get(url, headers=headers, timeout=15, allow_redirects=True)
            if resp.status_code in {403, 429, 503}:
                logger.debug("LoopNet direct fetch blocked (%s) for %r", resp.status_code, address)
                return None
            resp.raise_for_status()
        except requests.exceptions.RequestException:
            logger.debug("LoopNet direct fetch failed for %r", address)
            return None

        return self._parse_search_page(resp.text, url)

    def _parse_search_page(self, html: str, search_url: str) -> dict[str, Any] | None:
        """Parse listing cards from the LoopNet search results HTML."""
        try:
            from defusedxml.lxml import fromstring as defused_fromstring
            from lxml.etree import HTMLParser, LxmlError  # nosec B410 — HTMLParser is config only; parsing uses defused_fromstring
        except ImportError:
            return None

        try:
            parser = HTMLParser()
            tree = defused_fromstring(html.encode(), parser=parser)
        except LxmlError:
            logger.debug("LoopNet HTML parse failed")
            return None

        listings = []
        # LoopNet uses article elements with class "placard" for each listing
        cards = tree.cssselect("article.placard") if hasattr(tree, "cssselect") else []
        # Fallback: look for common listing title patterns
        if not cards:
            cards = tree.findall(".//*[@class='placard']") if tree is not None else []

        for card in cards[:5]:
            title_el = card.find(".//*[@class='placard-title']")
            addr_el = card.find(".//*[@class='placard-address']")
            price_el = card.find(".//*[@class='placard-price']")
            link_el = card.find(".//a[@href]")
            listing = {
                "title": (title_el.text_content() if hasattr(title_el, "text_content") else "").strip(),
                "address": (addr_el.text_content() if hasattr(addr_el, "text_content") else "").strip(),
                "price": (price_el.text_content() if hasattr(price_el, "text_content") else "").strip(),
                "url": self._absolute_url(link_el.get("href", "") if link_el is not None else ""),
            }
            if listing["title"] or listing["address"]:
                listings.append(listing)

        if not listings:
            return None

        return {"listings": listings, "search_url": search_url}

    def _web_search_fallback(self, address: str) -> dict[str, Any] | None:
        """
        Use the project's existing web-search gateway to find LoopNet pages.

        Returns parsed snippet data that can still surface property info.
        """
        try:
            from urbanlens.dashboard.services.search import get_search_gateway
        except ImportError:
            return None

        try:
            import requests.exceptions

            gateway = get_search_gateway()
            query = _LOOPNET_SITE_SEARCH.format(address=address)
            results = gateway.search(query)
        except (RuntimeError, requests.exceptions.RequestException, OSError):
            logger.debug("LoopNet web-search fallback failed for %r", address)
            return None

        if not results:
            return None

        listings = []
        for r in results[:_MAX_SEARCH_RESULTS]:
            url = r.get("url") or r.get("link") or ""
            if "loopnet.com" not in url:
                continue
            listings.append(
                {
                    "title": r.get("title", ""),
                    "address": address,
                    "snippet": _clean_snippet(r.get("snippet") or r.get("description") or ""),
                    "url": url,
                    "price": "",
                },
            )

        if not listings:
            return None

        return {"listings": listings, "search_url": "https://www.loopnet.com/search/"}

    def _absolute_url(self, href: str) -> str:
        if href.startswith("http"):
            return href
        return self.base_url + href


def _clean_snippet(text: str) -> str:
    """Strip HTML tags and collapse whitespace."""
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()
