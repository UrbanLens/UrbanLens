"""The one rule for a URL stored to be rendered as a link: http(s), well-formed, bounded.

A stored link ends up in an ``href``, where autoescaping does nothing against ``javascript:`` or ``data:``. Forms,
the external API, importers and provider data all write links, so each validates through :func:`clean_link_url`,
and the link models call it on save as the backstop.

This is not an SSRF guard: nothing here resolves the host. A URL the server will fetch goes through
``url_safety.ensure_public_http_url`` as well.
"""

from __future__ import annotations

import re

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator

#: Schemes a stored link may use.
LINK_URL_SCHEMES = ("http", "https")

_validate = URLValidator(schemes=list(LINK_URL_SCHEMES))

#: A leading ``scheme:`` - but not ``host:port``, which ``assume_https`` should still prefix.
_EXPLICIT_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:(?!\d)")


class InvalidLinkUrlError(ValueError):
    """The value is not a usable http(s) link; the message is for logs, not users."""


def clean_link_url(raw: object, *, max_length: int, assume_https: bool = False) -> str:
    """Return *raw* as a storable http(s) link.

    Args:
        raw: The submitted value.
        max_length: Longest accepted URL, after any ``https://`` prefix is added.
        assume_https: Read a scheme-less value (``example.com/a``) as ``https://`` rather than refusing it.

    Returns:
        The stripped (and possibly prefixed) URL.

    Raises:
        InvalidLinkUrlError: Empty, too long, not http(s), or malformed.
    """
    url = (str(raw) if raw is not None else "").strip()
    if not url:
        raise InvalidLinkUrlError("empty link")
    if assume_https and not _EXPLICIT_SCHEME.match(url):
        url = f"https://{url}"
    if len(url) > max_length:
        raise InvalidLinkUrlError(f"link is {len(url)} chars, over {max_length}")
    try:
        _validate(url)
    except ValidationError as exc:
        raise InvalidLinkUrlError(f"{url[:80]!r} is not an http(s) link") from exc
    return url


def is_link_url(value: object, *, max_length: int = 2000) -> bool:
    """Whether *value* would pass :func:`clean_link_url` unchanged, for render-side checks on stored rows."""
    try:
        return clean_link_url(value, max_length=max_length) == value
    except InvalidLinkUrlError:
        return False
