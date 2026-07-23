"""Best-effort parsing of the loose HTML/text descriptions found in KMZ/KML
placemarks (Google My Maps exports commonly embed an <img>, plain-text
key/value lines separated by <br>, and bare (unwrapped) URLs in the same
description field).
"""

from __future__ import annotations

import re

_BR_PATTERN = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TAG_PATTERN = re.compile(r"<[^>]+>")
_WHITESPACE_RUN_PATTERN = re.compile(r"[ \t]*\n[ \t]*(\n[ \t]*)+")

_IMG_SRC_PATTERN = re.compile(r'<img\b[^>]*\bsrc=["\']([^"\']+)["\']', re.IGNORECASE)
_A_HREF_PATTERN = re.compile(r'<a\b[^>]*\bhref=["\']([^"\']+)["\']', re.IGNORECASE)
# Trailing punctuation is excluded from the match itself (a URL at the end of
# a sentence shouldn't swallow its own period), and no <>"' - those either
# close an HTML attribute/tag or would already have been caught by the <a>/
# <img> patterns above.
_BARE_URL_PATTERN = re.compile(r'https?://[^\s<>"\')]+[^\s<>"\').,;:!?]')


def strip_html(text: str) -> str:
    """Strip HTML tags from *text*, keeping it readable.

    ``<br>`` becomes a newline (dropping it outright would run every line of
    a "City: X<br>State: Y<br>..." description together); every other tag is
    simply removed. Runs of 3+ newlines collapse to a single blank line.

    Args:
        text: Raw text that may contain HTML markup.

    Returns:
        Plain text with markup removed.
    """
    if not text:
        return text
    text = _BR_PATTERN.sub("\n", text)
    text = _TAG_PATTERN.sub("", text)
    text = _WHITESPACE_RUN_PATTERN.sub("\n\n", text)
    return text.strip()


def extract_image_urls(html: str) -> list[str]:
    """Return every ``<img src="...">`` URL in *html*, in document order, deduplicated.

    Args:
        html: Raw description text that may contain ``<img>`` tags.

    Returns:
        Ordered, deduplicated list of image URLs.
    """
    if not html:
        return []
    seen: set[str] = set()
    urls: list[str] = []
    for url in _IMG_SRC_PATTERN.findall(html):
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def extract_link_urls(html: str) -> list[str]:
    """Return every link URL in *html*: ``<a href="...">`` tags and bare ``http(s)://`` URLs in plain text.

    Image URLs (``<img src="...">``) are excluded - those are handled
    separately by :func:`extract_image_urls` and materialized as photos, not
    added as links.

    Args:
        html: Raw description text that may contain markup and/or bare URLs.

    Returns:
        Ordered, deduplicated list of link URLs.
    """
    if not html:
        return []
    image_urls = set(extract_image_urls(html))
    seen: set[str] = set()
    urls: list[str] = []
    for url in _A_HREF_PATTERN.findall(html) + _BARE_URL_PATTERN.findall(html):
        if url not in seen and url not in image_urls:
            seen.add(url)
            urls.append(url)
    return urls
