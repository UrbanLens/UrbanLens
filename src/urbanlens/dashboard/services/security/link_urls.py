"""The one rule for a URL stored to be rendered as a link: http(s), no user part, a host with a real top-level domain, bounded.

A stored link ends up in an ``href``, where autoescaping does nothing against ``javascript:`` or ``data:``. Forms,
the external API, importers and provider data all write links, so each validates through :func:`clean_link_url`,
and the link models call it on save as the backstop.

This is not an SSRF guard: nothing here resolves the host. A URL the server will fetch goes through
``url_safety.ensure_public_http_url`` as well.
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator

#: Schemes a stored link may use.
LINK_URL_SCHEMES = ("http", "https")

_validate = URLValidator(schemes=list(LINK_URL_SCHEMES))

#: A leading ``scheme:`` - but not ``host:port``, which still gets ``https://``.
_EXPLICIT_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:(?!\d)")
_TOP_LEVEL_DOMAIN = re.compile(r"^(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})$")


class InvalidLinkUrlError(ValueError):
    """The value is not a usable http(s) link; the message is for logs, not users."""


def _has_top_level_domain(url: str) -> bool:
    host = (urlsplit(url).hostname or "").rstrip(".")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return False
    labels = host.split(".")
    return len(labels) > 1 and all(labels) and bool(_TOP_LEVEL_DOMAIN.match(labels[-1]))


def clean_link_url(raw: object, *, max_length: int) -> str:
    """Return *raw* as a storable http(s) link, reading a value with no scheme (``example.com/a``) as ``https://``.

    Args:
        raw: The submitted value.
        max_length: Longest accepted URL, after any ``https://`` prefix is added.

    Returns:
        The stripped (and possibly prefixed) URL.

    Raises:
        InvalidLinkUrlError: Empty, too long, not http(s), malformed, names a user, or its host has no top-level domain.
    """
    url = (str(raw) if raw is not None else "").strip()
    if not url:
        raise InvalidLinkUrlError("empty link")
    if not _EXPLICIT_SCHEME.match(url):
        url = f"https://{url}"
    if "@" in urlsplit(url).netloc:
        raise InvalidLinkUrlError(f"{url[:80]!r} names a user, so it would show one host and open another")
    if len(url) > max_length:
        raise InvalidLinkUrlError(f"link is {len(url)} chars, over {max_length}")
    try:
        _validate(url)
    except ValidationError as exc:
        raise InvalidLinkUrlError(f"{url[:80]!r} is not an http(s) link") from exc
    if not _has_top_level_domain(url):
        raise InvalidLinkUrlError(f"{url[:80]!r} has no top-level domain")
    return url


def is_link_url(value: object, *, max_length: int = 2000) -> bool:
    """Whether *value* would pass :func:`clean_link_url` unchanged, for render-side checks on stored rows."""
    try:
        return clean_link_url(value, max_length=max_length) == value
    except InvalidLinkUrlError:
        return False
