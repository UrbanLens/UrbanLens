"""Seed a wiki's or pin's article from a confidently-matched Wikipedia article."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

# Only ever parses markup that WikipediaGateway has already run through
# nh3.clean() against its own fixed tag allowlist (see _ALLOWED_TAGS in
# services/apis/assets/wikipedia.py) - never raw/untrusted HTML.
import lxml.html as lxml_html  # nosec B410

from urbanlens.dashboard.models.article.model import EDIT_SUMMARY_IMAGES_LOCALIZED, EDIT_SUMMARY_SEEDED_FROM_WIKIPEDIA
from urbanlens.dashboard.services.security.url_safety import request_public_url

if TYPE_CHECKING:
    from lxml.html import HtmlElement

    from urbanlens.dashboard.models.article.model import Article
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.wiki.model import Wiki

logger = logging.getLogger(__name__)

_WIKIPEDIA_CACHE_SOURCE = "wikipedia"
_EDIT_SUMMARY = EDIT_SUMMARY_SEEDED_FROM_WIKIPEDIA
_COVER_MAX_BYTES = 8_000_000

#: A Markdown image's address, which ``localize_article_images`` rewrites and nothing else does.
_IMAGE_ADDRESS = re.compile(r"(!\[[^\]]*\])\([^)]*\)")

#: Markdown heading prefix for each heading tag WikipediaGateway's extract
#: allowlist permits (h2-h6 - Wikipedia extracts never carry an h1).
_HEADING_MD_PREFIX = {"h2": "##", "h3": "###", "h4": "####", "h5": "#####", "h6": "######"}


def takes_wikipedia_article(wiki: Wiki) -> bool:
    """Whether a Wikipedia match at the wiki's point may describe the wiki.

    Not for a campus building's wiki, nor anything else nested under another wiki without a property's place of its
    own (``name_tiers.wiki_scope``): the article found at its point is its campus's or a neighbour's
    (``name_tiers.describes_scope``).

    Args:
        wiki: The wiki.

    Returns:
        True when the wiki may carry the article and its link.
    """
    from urbanlens.dashboard.services.locations.name_tiers import NameTier, describes_scope, wiki_scope

    return describes_scope(NameTier.ENCYCLOPEDIA, wiki_scope(wiki))


def seed_wiki_article_from_wikipedia(location: Location) -> Article | None:
    """Write the wiki's first article from a cached Wikipedia match, if applicable.

    Args:
        location: The location to seed a wiki article for.

    Returns:
        The newly created Article, or None if nothing was seeded."""
    from urbanlens.dashboard.models.wiki.model import Wiki

    wiki = Wiki.objects.existing_for_location(location)
    if wiki is None or not takes_wikipedia_article(wiki):
        return None

    from urbanlens.dashboard.services.wiki.articles import get_article, save_article

    if get_article(wiki=wiki) is not None:
        return None

    content = _seed_content_for_location(location)
    if content is None:
        return None

    article, _revision = save_article(editor=None, content=content, edit_summary=_EDIT_SUMMARY, wiki=wiki)
    apply_wikipedia_cover_if_missing(location=location)
    logger.debug("Seeded wiki %s's article from Wikipedia", wiki.pk)
    return article


def is_untouched_wikipedia_seed(article: Article) -> bool:
    """Whether an article is still exactly what Wikipedia seeded, so nobody's writing is lost by removing it.

    Its first revision is the seed, and every later one is ``localize_article_images`` changing nothing but image
    addresses; no revision has an editor, and the article holds the last revision's text.

    Args:
        article: The article.

    Returns:
        True for an untouched seed.
    """
    if article.last_edited_by_id is not None:
        return False
    revisions = list(article.revisions.order_by("created", "pk").values_list("editor_id", "edit_summary", "content"))
    if not revisions or revisions[0][1] != EDIT_SUMMARY_SEEDED_FROM_WIKIPEDIA or revisions[-1][2] != article.content:
        return False
    if any(editor_id is not None for editor_id, _summary, _content in revisions):
        return False
    previous = revisions[0][2]
    for _editor_id, summary, content in revisions[1:]:
        if summary != EDIT_SUMMARY_IMAGES_LOCALIZED or _without_image_addresses(content) != _without_image_addresses(previous):
            return False
        previous = content
    return True


def _without_image_addresses(content: str) -> str:
    return _IMAGE_ADDRESS.sub(r"\1()", content)


def drop_misplaced_wikipedia_seed(wiki: Wiki) -> bool:
    """Remove a wiki's article when Wikipedia seeded it untouched and the wiki no longer takes one (it was nested).

    Args:
        wiki: The wiki, as it stands now.

    Returns:
        True when the article was removed.
    """
    from django.db import transaction

    from urbanlens.dashboard.models.article.model import Article

    if takes_wikipedia_article(wiki):
        return False
    with transaction.atomic():
        # Locked so an edit landing meanwhile waits, rather than being deleted along with the seed.
        article = Article.objects.select_for_update().filter(wiki_id=wiki.pk).first()
        if article is None or not is_untouched_wikipedia_seed(article):
            return False
        article.delete()
    logger.info("Removed wiki %s's Wikipedia seed: it describes the wiki it is nested under", wiki.pk)
    return True


def seed_pin_article_from_wikipedia(pin: Pin) -> Article | None:
    """Write a pin's first article from a cached Wikipedia match, if applicable.

    Args:
        pin: The pin to seed an article for.

    Returns:
        The newly created Article, or None if nothing was seeded."""
    if not pin.profile.auto_create_pin_article_from_wikipedia:
        return None

    location = pin.location
    if location is None:
        return None

    from urbanlens.dashboard.services.wiki.articles import get_article, save_article

    if get_article(pin=pin) is not None:
        return None

    content = _seed_content_for_location(location)
    if content is None:
        return None

    from urbanlens.dashboard.models.pin.model import Pin

    try:
        article, _revision = save_article(editor=None, content=content, edit_summary=_EDIT_SUMMARY, pin=pin)
    except Pin.DoesNotExist:
        return None
    apply_wikipedia_cover_if_missing(pin=pin)
    logger.debug("Seeded pin %s's article from Wikipedia", pin.pk)
    return article


def seed_pin_from_cached_wikipedia(pin: Pin) -> None:
    """Give a pin the article and link its location's cached Wikipedia match offers.

    Called only for the pin owner's own activity: a match another account's lookup cached reaches
    this pin when its owner next opens it.

    Args:
        pin: The pin to seed.
    """
    from urbanlens.dashboard.models.cache.location_cache import LocationCache
    from urbanlens.dashboard.models.links.model import AutoLinkSource
    from urbanlens.dashboard.services.locations.external_links import add_pin_link

    if pin.location_id is None:
        return
    cached = LocationCache.objects.filter(location_id=pin.location_id, source=_WIKIPEDIA_CACHE_SOURCE).first()
    if cached is None or not (cached.data or {}).get("title"):
        return
    seed_pin_article_from_wikipedia(pin)
    if url := cached.data.get("url"):
        add_pin_link(pin, url, "Wikipedia", source=AutoLinkSource.WIKIPEDIA)


def _seed_content_for_location(location: Location) -> str | None:
    """Build seed-ready Markdown from the location's cached Wikipedia match, if any.

    Args:
        location: The location whose cached "wikipedia" LocationCache row to read.

    Returns:
        Markdown content (body + attribution footer), or None when there's no usable cached match to seed from."""
    from urbanlens.dashboard.models.cache.location_cache import LocationCache

    cached = LocationCache.objects.filter(location=location, source=_WIKIPEDIA_CACHE_SOURCE).first()
    if cached is None or not cached.data:
        return None

    extract_html = (cached.data.get("extract") or "").strip()
    if not extract_html:
        return None

    body = _extract_html_to_markdown(extract_html).strip()
    if not body:
        return None

    blocks = [block for block in (_lead_image_markdown(cached.data), _infobox_markdown(cached.data.get("infobox")), body) if block]
    body = "\n\n".join(blocks)

    return f"{body}\n\n{_attribution_line(cached.data)}".strip()


def apply_wikipedia_cover_if_missing(*, pin: Pin | None = None, location: Location | None = None) -> None:
    """Use the Wikipedia lead image as the cover when the pin or wiki has none yet.

    An existing cover is left alone. A failed download does not affect the article.

    Args:
        pin: The pin whose cover may be set. Its location supplies the cached article.
        location: The wiki location whose cover may be set, when there is no pin.
    """
    target_location = pin.location if pin is not None else location
    if target_location is None:
        return
    if pin is not None and pin.cover_photo_id is not None:
        return
    from urbanlens.dashboard.models.cache.location_cache import LocationCache
    from urbanlens.dashboard.models.wiki.model import Wiki

    wiki = None if pin is not None else Wiki.objects.existing_for_location(target_location)
    if wiki is not None and wiki.cover_photo_id is not None:
        return
    cached = LocationCache.objects.filter(location=target_location, source=_WIKIPEDIA_CACHE_SOURCE).first()
    url = ((cached.data or {}).get("thumbnail") or "").strip() if cached is not None else ""
    if not url:
        return
    import requests

    from urbanlens.dashboard.services.photos.photo_upload import PhotoUploadError

    try:
        _store_cover_from_url(url, pin=pin, wiki=wiki)
    except (OSError, ValueError, requests.RequestException, PhotoUploadError):
        logger.debug("Wikipedia lead image was not saved as a cover", exc_info=True)


def _store_cover_from_url(url: str, *, pin: Pin | None, wiki: Wiki | None) -> None:
    """Download one image and set it as the pin or wiki cover."""
    from django.core.files.uploadedfile import SimpleUploadedFile

    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.services.photos.photo_upload import upload_photo

    owner = pin.profile if pin is not None else None
    if owner is None:
        # TODO: a wiki cover has no owning profile for upload_photo; decide who owns it before fetching one.
        return
    response = request_public_url("GET", url, timeout=8, max_bytes=_COVER_MAX_BYTES)
    response.raise_for_status()
    content = response.content
    if not content:
        return
    name = url.rsplit("/", 1)[-1].split("?", 1)[0] or "wikipedia-cover.jpg"
    if "." not in name:
        name = f"{name}.jpg"
    image = upload_photo(owner, SimpleUploadedFile(name, content, content_type=response.headers.get("Content-Type", "image/jpeg")), caption="Wikipedia", pin=pin)
    if pin is not None and pin.cover_photo_id is None:
        pin.cover_photo = image
        pin.save(update_fields=["cover_photo"])
    if wiki is not None and wiki.cover_photo_id is None:
        Image.objects.filter(pk=image.pk).update(wiki=wiki)
        wiki.cover_photo = image
        wiki.save(update_fields=["cover_photo"])


def _lead_image_markdown(article_data: dict) -> str:
    """Render the article's lead thumbnail (already cached alongside the extract) as a Markdown image of this site's copy.

    Args:
        article_data: The cached Wikipedia article dict (``title``/``thumbnail``).

    Returns:
        A Markdown image block, or "" when there's no thumbnail cached."""
    from urbanlens.dashboard.services.media.remote_copies import copy_url

    url = (article_data.get("thumbnail") or "").strip()
    if not url:
        return ""
    alt = (article_data.get("title") or "Wikipedia lead image").replace("[", "(").replace("]", ")")
    return f"![{alt}]({copy_url(url, provider='wikipedia', page_url=article_data.get('url') or '')})"


def _infobox_markdown(pairs: object) -> str:
    """Render a Wikipedia infobox's label/value fact pairs as a Markdown bullet list.

    Args:
        pairs: The cached ``infobox`` value (``list[list[str]]`` when present) - typed loosely since it comes back out of a JSONField and may be missing/None for a location cached before this field existed, or genuinely empty when the article had no infobox.

    Returns:
        A Markdown bullet list, or "" if there are no usable pairs."""
    if not isinstance(pairs, list):
        return ""
    lines: list[str] = []
    for pair in pairs:
        if not isinstance(pair, list) or len(pair) != 2:
            continue
        label = " ".join(str(pair[0]).split())
        value = " ".join(str(pair[1]).split())
        if not label or not value:
            continue
        lines.append(f"- **{label}:** {value}")
    return "\n".join(lines)


def _attribution_line(article_data: dict) -> str:
    """Build the CC BY-SA attribution footer required to reuse Wikipedia content.

    Args:
        article_data: The cached Wikipedia article dict (``title``/``url``).

    Returns:
        A Markdown footer crediting the source article, or "" if there's no URL to link (shouldn't happen for a real match, but content without attribution should never be seeded)."""
    url = article_data.get("url") or ""
    if not url:
        return ""
    title = article_data.get("title") or ""
    suffix = f" ({title})" if title else ""
    return f"---\n\n*This article was started from [Wikipedia]({url}){suffix}, licensed under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/). Feel free to expand and edit it.*"


def _extract_html_to_markdown(html: str) -> str:
    """Convert a WikipediaGateway extract to Markdown source.
    The input is always sanitized HTML restricted to a small, known tag set (see ``_ALLOWED_TAGS`` in ``services.apis.assets.wikipedia``) - this only needs to handle exactly those tags, not arbitrary HTML.

    Args:
        html: The extract HTML (e.g. ``LocationCache`` row's ``data["extract"]``).

    Returns:
        Markdown source, blocks separated by blank lines."""
    root = lxml_html.fromstring(f"<div>{html}</div>")
    blocks: list[str] = []
    for el in root:
        tag = el.tag
        if tag == "p":
            text = _inline_markdown(el).strip()
            if text:
                blocks.append(text)
        elif tag in _HEADING_MD_PREFIX:
            text = _inline_markdown(el).strip()
            if text:
                blocks.append(f"{_HEADING_MD_PREFIX[tag]} {text}")
        elif tag in ("ul", "ol"):
            block = _list_markdown(el, ordered=tag == "ol")
            if block:
                blocks.append(block)
        elif tag == "dl":
            block = _definition_list_markdown(el)
            if block:
                blocks.append(block)
        elif tag == "blockquote":
            text = _inline_markdown(el).strip()
            if text:
                blocks.append("\n".join(f"> {line}" for line in text.splitlines()))
        # Any other allowed tag (b/i/em/strong/sup/sub/br) only ever appears
        # nested inline, never as a direct child of the wrapping <div> -
        # nothing else to handle at the block level.
    return "\n\n".join(blocks)


def _list_markdown(list_el: HtmlElement, *, ordered: bool) -> str:
    """Render a <ul>/<ol>'s direct <li> children as Markdown list lines."""
    lines: list[str] = []
    for index, li in enumerate(list_el.findall("li"), start=1):
        text = _inline_markdown(li).strip()
        if not text:
            continue
        marker = f"{index}." if ordered else "-"
        lines.append(f"{marker} {text}")
    return "\n".join(lines)


def _definition_list_markdown(dl_el: HtmlElement) -> str:
    """Render a <dl>'s <dt>/<dd> children as bold-term/colon-definition lines."""
    lines: list[str] = []
    for child in dl_el:
        text = _inline_markdown(child).strip()
        if not text:
            continue
        if child.tag == "dt":
            lines.append(f"**{text}**")
        elif child.tag == "dd":
            lines.append(f": {text}")
    return "\n".join(lines)


def _inline_markdown(el: HtmlElement) -> str:
    """Render one element's text + inline children (b/i/em/strong/sup/sub/br) to Markdown.
    sup/sub have no Markdown equivalent - kept as plain text rather than emitting raw HTML the renderer would otherwise escape literally (see services.wiki.articles.render_article, which doesn't enable raw HTML passthrough)."""
    parts: list[str] = []
    if el.text:
        parts.append(el.text)
    for child in el:
        tag = child.tag
        if tag == "br":
            parts.append("  \n")
        elif tag in ("b", "strong"):
            parts.append(f"**{_inline_markdown(child).strip()}**")
        elif tag in ("i", "em"):
            parts.append(f"*{_inline_markdown(child).strip()}*")
        else:
            parts.append(_inline_markdown(child))
        if child.tail:
            parts.append(child.tail)
    return "".join(parts)
