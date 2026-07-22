"""Public Flickr photo search for the pin/wiki Media gallery.

Distinct from ``gateway.py`` (one user's own OAuth1 library) and ``public.py``
(a single pasted public album) - this searches Flickr's entire public photo
pool using a query built from every name this site knows for the place, plus
two required qualifiers: the state, and a fixed set of urbex-context terms.
Both are required because a plain landmark-name query against Flickr's global
pool returns a lot of photos that merely share a common word with the name -
the state qualifier keeps a name that also belongs to a same-named place
elsewhere from matching, and the urbex terms keep results on-topic for this
site's subject matter.

Two gateways implement this, chosen automatically by :class:`FlickrPlugin`
based on whether a Flickr API key is configured:

* :class:`FlickrSearchGateway` - ``flickr.photos.search`` full-text search,
  requires an API key. Understands quoted phrases and boolean OR, so the
  query is one combined ``(names) "state" (urbex terms)`` string.
* :class:`FlickrFeedSearchGateway` - Flickr's public tags syndication feed,
  which has never required a key. It only supports ANDing/ORing literal
  normalized tags, not free-text boolean search, so the same requirement is
  instead expressed as several simple 3-tag AND queries (see
  :func:`build_feed_tag_queries`) whose results get unioned. Meaningfully
  weaker than the API: it only surfaces *recent* uploads matching a tag (not
  a search over Flickr's history), matching is exact-tag rather than
  full-text relevance, and only a small preview image is available, not a
  full-resolution original.

Both contribute the same Media gallery tab (see ``services.external_data``)
alongside Wikimedia/Smithsonian/LOC/Internet Archive, and share the
``flickr`` rate-limit/usage-tracking service key with the other two Flickr
integrations.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.services.apis.assets.base import MediaItem, MediaProvider
from urbanlens.dashboard.services.apis.flickr.oauth import FlickrNotConfiguredError, _consumer_credentials
from urbanlens.dashboard.services.apis.flickr.public import photo_web_url
from urbanlens.dashboard.services.external_data import MediaPanelSource

if TYPE_CHECKING:
    from collections.abc import Generator

    from urbanlens.dashboard.models.pin.model import Pin

logger = logging.getLogger(__name__)

REST_ENDPOINT = "https://api.flickr.com/services/rest/"
_REQUEST_TIMEOUT = 20
_PER_PAGE = 40
_EXTRAS = "url_s,url_z,url_c,url_l,url_o"

FEED_ENDPOINT = "https://www.flickr.com/services/feeds/photos_public.gne"
_FEED_TIMEOUT = 15
# Caps how many distinct names one panel fetch queries against the public
# feed - each name is crossed with every URBEX_TERMS entry (see
# build_feed_tag_queries), so this bounds the request fan-out per pin fetch
# against the shared "flickr" rate-limit budget.
_FEED_MAX_NAMES = 4

# Required, ANDed onto every query (see build_search_query and
# build_feed_tag_queries) so a global search stays scoped to this site's
# subject matter rather than any photo that happens to share a word with the
# place's name.
URBEX_TERMS: tuple[str, ...] = ("abandoned", "urbex", "urban exploration")


def _quoted_or_group(terms: list[str]) -> str:
    """Build a parenthesized, quoted OR clause from ``terms`` (deduped, order preserved).

    Args:
        terms: Candidate phrases; blank entries are skipped and duplicates
            (case-insensitive) are collapsed to their first occurrence.

    Returns:
        ``'("a" OR "b")'``, or ``""`` when no term survives.
    """
    seen: set[str] = set()
    quoted: list[str] = []
    for raw in terms:
        # Strip embedded quotes rather than escaping them - Flickr's query
        # syntax has no escape mechanism, and a stray quote would otherwise
        # unbalance the group for every term after it.
        term = raw.strip().replace('"', "")
        if not term:
            continue
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        quoted.append(f'"{term}"')
    return "(" + " OR ".join(quoted) + ")" if quoted else ""


@dataclass(frozen=True, slots=True)
class _QueryComponents:
    """The raw ingredients of a pin's required-operator Flickr query, before rendering."""

    names: list[str]
    state: str


def _search_components(pin: Pin) -> _QueryComponents | None:
    """Gather the pin's known names and state, shared by both query renderers.

    Args:
        pin: The pin to gather query components for.

    Returns:
        The raw (undeduped, unquoted) names and state, or None when the pin
        has no known state at all - both renderers require one.
    """
    from django.core.exceptions import ObjectDoesNotExist

    from urbanlens.dashboard.services.locations.naming import is_address_derived_name, is_meaningful_name

    location = pin.location
    state = pin.effective_state
    if not state:
        return None

    names: list[str] = []
    for name in (pin.meaningful_official_name, pin.meaningful_name):
        if name:
            names.append(name)

    for alias in pin.aliases.all():
        if alias.is_nickname or not is_meaningful_name(alias.name):
            continue
        names.append(alias.name)

    if location is not None:
        try:
            wiki = location.wiki
        except ObjectDoesNotExist:
            wiki = None
        if wiki is not None:
            for wiki_alias in wiki.aliases.all():
                if wiki_alias.is_nickname or not is_meaningful_name(wiki_alias.name):
                    continue
                names.append(wiki_alias.name)
        # A name that's really just a fragment of the address (a street name,
        # the city) carries no landmark-identifying power and would only add
        # noise to the query.
        names = [name for name in names if not is_address_derived_name(name, location)]

    return _QueryComponents(names=names, state=state)


def build_search_query(pin: Pin) -> str | None:
    """Build the required-operator Flickr full-text search query for a pin.

    Used by :class:`FlickrSearchGateway` (the API-backed provider). The query
    has the shape ``(names) "state" (urbex terms)``: an OR-group of every
    meaningful, non-nickname name known for the place, ANDed with the pin's
    state in quotes and the fixed :data:`URBEX_TERMS` OR-group. Both the name
    group and the state are required - a pin with neither is skipped rather
    than searched with a weaker query, since a Flickr full-text search
    without them returns mostly off-topic noise.

    Args:
        pin: The pin to build a search query for.

    Returns:
        The query string, or None when the pin has no usable name or no
        known state.
    """
    components = _search_components(pin)
    if components is None:
        return None

    names_clause = _quoted_or_group(components.names)
    if not names_clause:
        return None

    urbex_clause = _quoted_or_group(list(URBEX_TERMS))
    return f'{names_clause} "{components.state}" {urbex_clause}'


def build_feed_tag_queries(pin: Pin) -> list[str]:
    """Decompose a pin's required-operator query into public-feed tag-AND queries.

    Used by :class:`FlickrFeedSearchGateway` (the keyless fallback). The
    public syndication feed only supports ANDing/ORing literal, normalized
    tags - not free-text boolean search - so the same "(names) state (urbex
    terms)" requirement is instead expressed as several 3-tag AND queries
    (one name x one urbex term, both ANDed with the state). Running all of
    them and unioning the results (see ``MediaProvider.get_media``) is
    mathematically equivalent to the API's single OR-of-names AND state AND
    OR-of-urbex-terms query - just decomposed into queries the feed can
    actually run. Recall is still much lower than the API-backed provider,
    since matching is exact-normalized-tag rather than full-text relevance.

    Args:
        pin: The pin to build tag queries for.

    Returns:
        Comma-joined 3-tag strings (name, state, urbex term; each normalized
        the same way Flickr normalizes tags for matching), or ``[]`` when the
        pin has no usable name or state. Capped at :data:`_FEED_MAX_NAMES`
        distinct names to bound how many feed requests one fetch issues.
    """
    from urbanlens.dashboard.services.locations.naming import normalize_name_for_comparison

    components = _search_components(pin)
    if components is None:
        return []

    state_tag = normalize_name_for_comparison(components.state)
    if not state_tag:
        return []

    name_tags: list[str] = []
    seen: set[str] = set()
    for name in components.names:
        tag = normalize_name_for_comparison(name)
        if not tag or tag in seen:
            continue
        seen.add(tag)
        name_tags.append(tag)
        if len(name_tags) >= _FEED_MAX_NAMES:
            break
    if not name_tags:
        return []

    urbex_tags = [normalize_name_for_comparison(term) for term in URBEX_TERMS]
    return [f"{name_tag},{state_tag},{urbex_tag}" for name_tag in name_tags for urbex_tag in urbex_tags]


@dataclass(slots=True, kw_only=True)
class FlickrSearchGateway(MediaProvider):
    """Unauthenticated ``flickr.photos.search`` client for the Media gallery."""

    service_key: ClassVar[str] = "flickr"
    display_name: ClassVar[str] = "Flickr"
    paid_service: ClassVar[bool] = False

    def _search(self, text: str) -> list[dict[str, Any]]:
        """Run one ``flickr.photos.search`` call and return raw photo dicts.

        Failures (including a site with no Flickr API key configured) are
        swallowed and logged rather than raised, matching the other Media
        gallery providers - a per-term search failure should degrade to "no
        results from this term", not suppress the whole panel.

        Args:
            text: The full boolean query text, from :func:`build_search_query`.

        Returns:
            Raw photo dicts from Flickr's response, or ``[]`` on any failure.
        """
        try:
            api_key, _secret = _consumer_credentials()
        except FlickrNotConfiguredError:
            return []
        params = {
            "method": "flickr.photos.search",
            "format": "json",
            "nojsoncallback": "1",
            "api_key": api_key,
            "text": text,
            "media": "photos",
            "content_type": "1",
            "sort": "relevance",
            "per_page": str(_PER_PAGE),
            "extras": _EXTRAS,
        }
        try:
            response = self.session.get(REST_ENDPOINT, params=params, timeout=_REQUEST_TIMEOUT)
            response.raise_for_status()
            body = response.json()
        except Exception:
            # TODO: Catch specific exceptions
            logger.exception("Flickr search failed for %r", text)
            return []
        if body.get("stat") != "ok":
            logger.warning("Flickr search API returned an error for %r: %s", text, body)
            return []
        return body.get("photos", {}).get("photo", [])

    def _generate_media(self, search_term: str, address: str | None = None) -> Generator[MediaItem]:
        if not search_term:
            return
        for photo in self._search(search_term):
            url = photo.get("url_o") or photo.get("url_l") or photo.get("url_c") or photo.get("url_z") or photo.get("url_s")
            if not url:
                continue
            owner = photo.get("owner")
            photo_id = photo.get("id")
            yield MediaItem(
                url=url,
                thumb_url=photo.get("url_s") or photo.get("url_z") or url,
                caption=photo.get("title") or "",
                source=self.display_name,
                page_url=photo_web_url(owner, photo_id) if owner and photo_id else "",
            )


@dataclass(slots=True, kw_only=True)
class FlickrFeedSearchGateway(MediaProvider):
    """Keyless fallback: Flickr's public tags syndication feed.

    Used automatically in place of :class:`FlickrSearchGateway` when the site
    has no Flickr API key configured (see ``FlickrPlugin.get_panel_sources``)
    - Flickr's public feeds have never required a key. Meaningfully weaker
    than the API-backed search though (see the module docstring): recent
    uploads only, exact-tag matching, and only a small preview image per
    photo. Switches back to the API-backed gateway automatically the moment a
    key is configured - see ``FlickrMediaPanelSource.search_terms``.
    """

    service_key: ClassVar[str] = "flickr"
    display_name: ClassVar[str] = "Flickr"
    paid_service: ClassVar[bool] = False

    def _fetch_tagged(self, tags_csv: str) -> list[dict[str, Any]]:
        """Run one public-feed request for a comma-joined, ANDed tag set.

        Args:
            tags_csv: A ``"tag1,tag2,tag3"`` string from
                :func:`build_feed_tag_queries`.

        Returns:
            The feed's raw item dicts, or ``[]`` on any failure.
        """
        params = {"tags": tags_csv, "tagmode": "all", "format": "json", "nojsoncallback": "1"}
        try:
            response = self.session.get(FEED_ENDPOINT, params=params, timeout=_FEED_TIMEOUT)
            response.raise_for_status()
            body = response.json()
        except Exception:
            # TODO: Catch specific exceptions
            logger.exception("Flickr public feed request failed for tags=%r", tags_csv)
            return []
        return body.get("items", [])

    def _generate_media(self, search_term: str, address: str | None = None) -> Generator[MediaItem]:
        if not search_term:
            return
        for item in self._fetch_tagged(search_term):
            media = item.get("media") or {}
            url = media.get("m")
            if not url:
                continue
            link = item.get("link") or ""
            author_id = item.get("author_id") or ""
            # The feed's own `link` uses the account's path-alias username
            # (when it has one) rather than its NSID, so it wouldn't always
            # match the NSID-based source_url the other two Flickr paths
            # store for the same photo (see photo_web_url) - rebuilding it
            # from author_id keeps the dedup key consistent across all three.
            photo_id = link.rstrip("/").rsplit("/", 1)[-1] if link else ""
            page_url = photo_web_url(author_id, photo_id) if author_id and photo_id.isdigit() else link
            yield MediaItem(
                url=url,
                thumb_url=url,
                caption=item.get("title") or "",
                source=self.display_name,
                page_url=page_url,
            )


class FlickrMediaPanelSource(MediaPanelSource):
    """Flickr's Media gallery provider: a required-operator, urbex-scoped query.

    Overrides the generic ``MediaPanelSource.search_terms`` (built from
    ``pin.get_unique_search_name``) - a plain landmark-name query returns too
    much off-topic noise against Flickr's global photo pool. Which query
    shape gets built depends on which gateway is active (see
    ``FlickrPlugin.get_panel_sources``): :func:`build_search_query`'s single
    full-text string for the API-backed :class:`FlickrSearchGateway`, or
    :func:`build_feed_tag_queries`'s decomposed tag-AND queries for the
    keyless :class:`FlickrFeedSearchGateway`.
    """

    @staticmethod
    def search_terms(pin: Pin, gateway: MediaProvider) -> list[str]:
        """This pin's Flickr query terms, shaped for whichever gateway is active.

        Args:
            pin: The pin to build search queries for.
            gateway: The active gateway - determines which query shape to build.

        Returns:
            The query terms for ``gateway``, or ``[]`` when the pin has no
            usable name or state.
        """
        if isinstance(gateway, FlickrFeedSearchGateway):
            return build_feed_tag_queries(pin)
        query = build_search_query(pin)
        return [query] if query else []
