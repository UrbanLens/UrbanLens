"""Seed a wiki's article from a confidently-matched Wikipedia article.

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
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

# Only ever parses markup that WikipediaGateway has already run through
# nh3.clean() against its own fixed tag allowlist (see _ALLOWED_TAGS in
# services/apis/assets/wikipedia.py) - never raw/untrusted HTML.
import lxml.html as lxml_html  # nosec B410

if TYPE_CHECKING:
    from lxml.html import HtmlElement

    from urbanlens.dashboard.models.article.model import Article
    from urbanlens.dashboard.models.location.model import Location

logger = logging.getLogger(__name__)

_WIKIPEDIA_CACHE_SOURCE = "wikipedia"
_EDIT_SUMMARY = "Seeded from Wikipedia"

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

    content = f"{body}\n\n{_attribution_line(cached.data)}".strip()
    article, _revision = save_article(editor=None, content=content, edit_summary=_EDIT_SUMMARY, wiki=wiki)
    logger.info("Seeded wiki %s's article from Wikipedia article %r", wiki.pk, cached.data.get("title"))
    return article


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
    return (
        f"---\n\n*This article was started from [Wikipedia]({url}){suffix}, "
        "licensed under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/). "
        "Feel free to expand and edit it.*"
    )


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
