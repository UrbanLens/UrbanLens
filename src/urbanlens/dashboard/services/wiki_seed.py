"""Seed a wiki's or pin's article from a confidently-matched Wikipedia article.

Wikis are created empty (see ``services.locations.creation.WikiCreationService``'s
docstring: "Wikis are never created automatically") - but once a Wikipedia
article has been confidently matched to the wiki's location (see
``WikipediaGateway.get_article_for_location``, which only ever returns a
candidate that passed its own address/title verification), that's a natural
starting point rather than an empty page. This module turns the cached
match into the wiki's initial article, once, the first time either becomes
true:

- a Wikipedia match is (re)cached for a location that already has a wiki
  with no article yet (see ``models.cache.signals``), or
- a wiki is created for a location that already has a cached Wikipedia
  match (see ``services.locations.creation.WikiCreationService``).

Never overwrites: any existing Article row (seeded or human-written) is left
untouched - see ``seed_wiki_article_from_wikipedia``'s own guard.

``seed_pin_article_from_wikipedia`` does the same thing for a single pin,
gated on the pin owner's own ``Profile.auto_create_pin_article_from_wikipedia``
setting (on by default) - unlike a community wiki, a pin's article is
private to its owner, so seeding it is opt-out per-user rather than
something that always happens.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

# Only ever parses markup that WikipediaGateway has already run through
# nh3.clean() against its own fixed tag allowlist (see _ALLOWED_TAGS in
# services/apis/assets/wikipedia.py) - never raw/untrusted HTML.
import lxml.html as lxml_html  # nosec B410

from urbanlens.dashboard.models.article.model import EDIT_SUMMARY_SEEDED_FROM_WIKIPEDIA

if TYPE_CHECKING:
    from lxml.html import HtmlElement

    from urbanlens.dashboard.models.article.model import Article
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin

logger = logging.getLogger(__name__)

_WIKIPEDIA_CACHE_SOURCE = "wikipedia"
_EDIT_SUMMARY = EDIT_SUMMARY_SEEDED_FROM_WIKIPEDIA

#: Markdown heading prefix for each heading tag WikipediaGateway's extract
#: allowlist permits (h2-h6 - Wikipedia extracts never carry an h1).
_HEADING_MD_PREFIX = {"h2": "##", "h3": "###", "h4": "####", "h5": "#####", "h6": "######"}


def seed_wiki_article_from_wikipedia(location: Location) -> Article | None:
    """Write the wiki's first article from a cached Wikipedia match, if applicable.

    No-ops (returns None) unless all of: the location has a wiki, that wiki
    has no article yet (seeded or human-written - never overwrites either),
    and a Wikipedia article is actually cached for the location with a
    non-empty extract.

    Args:
        location: The location to seed a wiki article for.

    Returns:
        The newly created Article, or None if nothing was seeded.
    """
    wiki = getattr(location, "wiki", None)
    if wiki is None:
        return None

    from urbanlens.dashboard.services.articles import get_article, save_article

    if get_article(wiki=wiki) is not None:
        return None

    content = _seed_content_for_location(location)
    if content is None:
        return None

    article, _revision = save_article(editor=None, content=content, edit_summary=_EDIT_SUMMARY, wiki=wiki)
    logger.info("Seeded wiki %s's article from Wikipedia", wiki.pk)
    return article


def seed_pin_article_from_wikipedia(pin: Pin) -> Article | None:
    """Write a pin's first article from a cached Wikipedia match, if applicable.

    No-ops (returns None) unless all of: the pin owner's
    ``auto_create_pin_article_from_wikipedia`` setting is on, the pin has a
    location, the pin has no article yet (seeded or human-written - never
    overwrites either), and a Wikipedia article is actually cached for the
    pin's location with a non-empty extract.

    Args:
        pin: The pin to seed an article for.

    Returns:
        The newly created Article, or None if nothing was seeded.
    """
    if not pin.profile.auto_create_pin_article_from_wikipedia:
        return None

    location = pin.location
    if location is None:
        return None

    from urbanlens.dashboard.services.articles import get_article, save_article

    if get_article(pin=pin) is not None:
        return None

    content = _seed_content_for_location(location)
    if content is None:
        return None

    article, _revision = save_article(editor=None, content=content, edit_summary=_EDIT_SUMMARY, pin=pin)
    logger.info("Seeded pin %s's article from Wikipedia", pin.pk)
    return article


def _seed_content_for_location(location: Location) -> str | None:
    """Build seed-ready Markdown from the location's cached Wikipedia match, if any.

    Args:
        location: The location whose cached "wikipedia" LocationCache row to read.

    Returns:
        Markdown content (body + attribution footer), or None when there's no
        usable cached match to seed from.
    """
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


def _lead_image_markdown(article_data: dict) -> str:
    """Render the article's lead thumbnail (already cached alongside the extract) as a Markdown image.

    Uses ``WikipediaGateway._normalise``'s own ``thumbnail`` field - no extra
    fetch - rather than pulling in the separate, multi-image
    ``get_article_media`` gallery source (used for the pin's Media tab, a
    different feature): a seeded article calls for the one image Wikipedia
    itself leads with, not every image on the page.

    Args:
        article_data: The cached Wikipedia article dict (``title``/``thumbnail``).

    Returns:
        A Markdown image block, or "" when there's no thumbnail cached.
    """
    url = (article_data.get("thumbnail") or "").strip()
    if not url:
        return ""
    alt = (article_data.get("title") or "Wikipedia lead image").replace("[", "(").replace("]", ")")
    return f"![{alt}]({url})"


def _infobox_markdown(pairs: object) -> str:
    """Render a Wikipedia infobox's label/value fact pairs as a Markdown bullet list.

    ``WikipediaGateway._fetch_infobox`` reaches Wikipedia's real rendered
    HTML (the only response of theirs that carries the infobox at all - the
    lead/extended extracts are both backed by an extension that strips
    tables before returning) and already reduces it to plain-text ``[label,
    value]`` pairs, skipping the infobox's own title row, section dividers,
    and any image/map-only row (the embedded Kartographer map has no
    Markdown equivalent) - this only needs to format what's left.

    A GFM table was tried first, but a Markdown table always needs a header
    row, and a blank one (there's no natural two-column header for an
    arbitrary facts list) renders as a visibly empty header row once parsed
    into the article editor - ProseMirror fills any truly empty cell with
    its own placeholder paragraph (the ``<tr><th>...<br
    class="ProseMirror-trailingBreak">...`` artifact reported against the
    seeded article). A bullet list has no such requirement.

    Args:
        pairs: The cached ``infobox`` value (``list[list[str]]`` when
            present) - typed loosely since it comes back out of a JSONField
            and may be missing/None for a location cached before this field
            existed, or genuinely empty when the article had no infobox.

    Returns:
        A Markdown bullet list, or "" if there are no usable pairs.
    """
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
        A Markdown footer crediting the source article, or "" if there's no
        URL to link (shouldn't happen for a real match, but content without
        attribution should never be seeded).
    """
    url = article_data.get("url") or ""
    if not url:
        return ""
    title = article_data.get("title") or ""
    suffix = f" ({title})" if title else ""
    return f"---\n\n*This article was started from [Wikipedia]({url}){suffix}, licensed under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/). Feel free to expand and edit it.*"


def _extract_html_to_markdown(html: str) -> str:
    """Convert a WikipediaGateway extract to Markdown source.

    The input is always sanitized HTML restricted to a small, known tag set
    (see ``_ALLOWED_TAGS`` in ``services.apis.assets.wikipedia``) - this only
    needs to handle exactly those tags, not arbitrary HTML.

    Args:
        html: The extract HTML (e.g. ``LocationCache`` row's ``data["extract"]``).

    Returns:
        Markdown source, blocks separated by blank lines.
    """
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

    sup/sub have no Markdown equivalent - kept as plain text rather than
    emitting raw HTML the renderer would otherwise escape literally (see
    services.articles.render_article, which doesn't enable raw HTML passthrough).
    """
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
