"""LoopNet service - best-effort commercial real-estate data for a location."""

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

# Matches the address-slug segment shared by /Listing/<slug>/<id> and
# /property/<slug>/<parcel-id> URLs, used to group search hits that describe
# the same underlying property.
_LISTING_SLUG_RE = re.compile(r"loopnet\.com/(?:listing|property)/([^/]+)/", re.IGNORECASE)


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

    # -- private ----------------------------------------------------------------

    def _direct_search(self, address: str) -> dict[str, Any] | None:
        """Try fetching the LoopNet search results page directly."""
        import requests.exceptions

        slug = quote_plus(address.replace(",", "").replace("  ", " "))
        url = _LOOPNET_SEARCH_TMPL.format(address=slug)
        headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
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

            # HTMLParser is config only; parsing uses defused_fromstring
            from lxml.etree import HTMLParser, LxmlError  # nosec B410
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
            from urbanlens.dashboard.services.search import search_web
        except ImportError:
            return None

        try:
            import requests.exceptions

            from urbanlens.dashboard.services.rate_limiter import RequestCancelledError

            query = _LOOPNET_SITE_SEARCH.format(address=address)
            results = search_web(query)
        except (RuntimeError, requests.exceptions.RequestException, OSError, RequestCancelledError):
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

        primary_listings, duplicate_links = self._dedupe_and_enrich(listings)

        return {
            "listings": primary_listings,
            "search_url": "https://www.loopnet.com/search/",
            "duplicate_links": duplicate_links,
        }

    def _dedupe_and_enrich(self, listings: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        """
        Collapse same-property search hits and enrich the survivor with scraped detail.

        Web-search hits for a single address often surface several LoopNet URLs
        (an active listing, a parcel/APN page, an expired listing, a duplicate on
        the bare domain) that all describe the same property. This keeps one
        "primary" listing per property (the most useful, per ``_listing_rank``),
        attaches structured data scraped from that property's parcel-record page
        when one is found, and turns every other URL in the group into a small
        labeled link instead of a full duplicate card.

        Args:
            listings: Listing dicts in original search-result order.

        Returns:
            A ``(primary_listings, duplicate_links)`` tuple. ``primary_listings``
            has one entry per distinct property, in first-seen order, optionally
            carrying a ``property_details`` mapping. ``duplicate_links`` is a
            flat list of ``{"label": ..., "url": ...}`` dicts for the remaining
            URLs across all groups.
        """
        primary_listings: list[dict[str, Any]] = []
        duplicate_links: list[dict[str, str]] = []
        for group in _group_listings(listings).values():
            primary = _select_primary(group)

            property_listing = _find_property_listing(group)
            if property_listing is not None:
                details = self._fetch_property_details(property_listing["url"])
                if details:
                    primary["property_details"] = details

            primary_listings.append(primary)
            duplicate_links.extend({"label": _listing_label(entry), "url": entry["url"]} for entry in group if entry is not primary)

        return primary_listings, duplicate_links

    def _fetch_property_details(self, url: str) -> dict[str, str] | None:
        """
        Fetch and parse the assessment data grid from a LoopNet property-record page.

        Args:
            url: The LoopNet ``/property/...`` page URL.

        Returns:
            An ordered mapping of field label (e.g. ``"APN/Parcel ID"``) to its
            displayed value, or None if the page couldn't be fetched or parsed.
        """
        import requests.exceptions

        headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
            "Accept-Language": "en-US,en;q=0.9",
        }
        try:
            resp = self.session.get(url, headers=headers, timeout=15, allow_redirects=True)
            if resp.status_code in {403, 429, 503}:
                logger.debug("LoopNet property page fetch blocked (%s) for %r", resp.status_code, url)
                return None
            resp.raise_for_status()
        except requests.exceptions.RequestException:
            logger.debug("LoopNet property page fetch failed for %r", url)
            return None

        return _parse_property_page(resp.text)

    def _absolute_url(self, href: str) -> str:
        if href.startswith("http"):
            return href
        return self.base_url + href


def _clean_snippet(text: str) -> str:
    """Strip HTML tags and collapse whitespace."""
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _listing_group_key(url: str) -> str:
    """
    Return a key identifying the property a LoopNet URL refers to.

    Args:
        url: A LoopNet listing or property URL.

    Returns:
        The address slug shared by ``/Listing/<slug>/...`` and
        ``/property/<slug>-<zip>/...`` URLs, lowercased with any trailing
        zip-code suffix removed so both URL shapes group together. Falls
        back to the lowercased URL if no slug can be extracted.
    """
    match = _LISTING_SLUG_RE.search(url)
    if not match:
        return url.lower()
    slug = match.group(1).lower()
    return re.sub(r"-\d{5}$", "", slug)


def _listing_rank(listing: dict[str, Any]) -> int:
    """
    Score a listing's usefulness for de-duplication (lower is better).

    Args:
        listing: A listing dict with ``url`` and ``snippet`` keys.

    Returns:
        0 for an active listing, 1 for a parcel/property info page, 2 for a
        listing whose snippet indicates it is no longer advertised.
    """
    if "no longer being advertised" in listing.get("snippet", "").lower():
        return 2
    if "/property/" in listing.get("url", "").lower():
        return 1
    return 0


def _group_listings(listings: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """
    Group listings that describe the same property, preserving first-seen order.

    Args:
        listings: Listing dicts in original search-result order.

    Returns:
        A mapping of ``_listing_group_key`` to the listings sharing that key,
        in insertion order.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for listing in listings:
        key = _listing_group_key(listing.get("url", ""))
        groups.setdefault(key, []).append(listing)
    return groups


def _select_primary(group: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the most useful listing in a duplicate group (ties go to the first-seen)."""
    return min(group, key=_listing_rank)


def _find_property_listing(group: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the group's parcel/property-record page listing, if any."""
    for entry in group:
        if "/property/" in entry.get("url", "").lower():
            return entry
    return None


def _listing_label(listing: dict[str, Any]) -> str:
    """Return a short, human-readable label for a secondary listing link."""
    if _listing_rank(listing) == 2:
        return "Archived listing"
    if "/property/" in listing.get("url", "").lower():
        return "Parcel record"
    return "Additional listing"


def _parse_property_page(html: str) -> dict[str, str] | None:
    """
    Parse the assessment key/value grid from a LoopNet property-record page.

    LoopNet renders each field as a ``[data-automation-id]`` cell containing a
    ``.assessment-key`` label and an ``.assessment-value`` value (e.g. APN,
    zoning, lot size, flood zone). This extracts them generically so it keeps
    working if LoopNet adds or reorders fields.

    Args:
        html: Raw HTML of the property-record page.

    Returns:
        An ordered mapping of field label to value, or None if the page had
        no recognizable data.
    """
    try:
        from defusedxml.lxml import fromstring as defused_fromstring

        # HTMLParser is config only; parsing uses defused_fromstring
        from lxml.etree import HTMLParser, LxmlError  # nosec B410
    except ImportError:
        return None

    try:
        parser = HTMLParser()
        tree = defused_fromstring(html.encode(), parser=parser)
    except (LxmlError, AttributeError):
        # defusedxml.lxml.fromstring raises AttributeError (not LxmlError) when
        # the underlying lxml parse yields no root element, e.g. for empty HTML.
        logger.debug("LoopNet property page parse failed")
        return None

    if tree is None:
        return None

    # lxml's .xpath() is part of its core (libxml2-backed) API and needs no
    # extra dependency, unlike .cssselect() which requires the separate
    # (here, uninstalled) `cssselect` package.
    details: dict[str, str] = {}
    for cell in tree.xpath(".//*[@data-automation-id]"):
        key_els = cell.xpath(".//*[contains(concat(' ', normalize-space(@class), ' '), ' assessment-key ')]")
        value_els = cell.xpath(".//*[contains(concat(' ', normalize-space(@class), ' '), ' assessment-value ')]")
        if not key_els or not value_els:
            continue
        key = "".join(key_els[0].itertext()).strip()
        value = "".join(value_els[0].itertext()).strip()
        if key and value:
            details[key] = value

    return details or None
