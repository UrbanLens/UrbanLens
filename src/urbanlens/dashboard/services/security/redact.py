"""Helpers for keeping sensitive values out of application logs.

Log tags here are **random tokens, not hashes of the value**. That is the whole
point: a hash - even a keyed one - is a function of its input, so anyone who can
guess the input can confirm it. Coordinates and place names are low-entropy
(you always know the rough region), which makes any derived tag invertible by
enumeration in seconds. A tag drawn from :func:`secrets.token_hex` has no
preimage to find, so no amount of guessing, no wordlist, and no leaked key
recovers the original.

This replaced an HMAC-SHA256 fingerprint keyed with a constant literal in this
file, so the key was readable by anyone who could read the source. Keyed or
not, that made the tag a pure function of a low-entropy input: enumerating
plausible coordinates against a known key recovers the original in seconds
(measured at ~200k candidates/sec, single-core, during review), so the tag
offered no confidentiality it claimed to.

The property the module actually needs is *correlation* - "this is the same
value as the one three lines up" - and a memoized random token preserves it.
The scope of that correlation is the process (bounded cache) rather than all
of history, which is the right scope for reading a log file anyway.

Prefer logging an opaque database handle (``pin.uuid``, ``location.pk``) where
one is in scope: an operator can resolve it deliberately, through an
access-controlled lookup, which a token cannot do.
"""

from __future__ import annotations

from collections import OrderedDict
import secrets
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

#: Parameter names whose values are secrets (API keys, tokens, passwords).
_SENSITIVE_PARAM_NAMES = frozenset({"key", "api_key", "apikey", "token", "secret", "password", "access_token"})

#: Parameter names whose values locate a place. Not exhaustive by design - see
#: :func:`redact_params`, which redacts anything it does not recognise.
_COORDINATE_PARAM_NAMES = frozenset(
    {
        "lat",
        "lng",
        "lon",
        "latitude",
        "longitude",
        "latlng",
        "gscoord",
        "loc",
        "coordinates",
        "bbox",
        "bounds",
        "center",
        "viewport",
        "locationbias",
    }
)

#: Parameter names known to carry nothing sensitive, passed through verbatim so
#: a log line keeps some diagnostic value. Everything not listed is redacted.
_PASSTHROUGH_PARAM_NAMES = frozenset(
    {
        "format",
        "limit",
        "offset",
        "page",
        "per_page",
        "count",
        "radius",
        "zoom",
        "size",
        "width",
        "height",
        "language",
        "lang",
        "locale",
        "units",
        "version",
        "v",
        "type",
        "kind",
        "source",
        "provider",
        "fields",
        "sort",
        "order",
        "start",
        "end",
        "date",
        "days",
    }
)

#: How many distinct values keep a stable tag at once. Beyond this the
#: least-recently-used value is forgotten and would draw a fresh tag if it
#: reappeared - acceptable for log correlation, and it bounds memory.
_TOKEN_CACHE_SIZE = 4096

_TOKENS: OrderedDict[str, str] = OrderedDict()
_TOKENS_LOCK = threading.Lock()


def _tag(value: str) -> str:
    """Return a stable random token for ``value``.

    The same value yields the same token for as long as it stays in the cache,
    so repeated log lines still correlate. The token is drawn from the CSPRNG
    and is *not* derived from the value, so it cannot be inverted.

    Args:
        value: The sensitive value to stand in for.

    Returns:
        Eight hex characters of randomness.
    """
    with _TOKENS_LOCK:
        token = _TOKENS.get(value)
        if token is not None:
            _TOKENS.move_to_end(value)
            return token
        token = secrets.token_hex(4)
        _TOKENS[value] = token
        if len(_TOKENS) > _TOKEN_CACHE_SIZE:
            _TOKENS.popitem(last=False)
        return token


def redact_secret(value: str | None) -> str:
    """Return a log-safe token standing in for an API key, token, or secret.

    Args:
        value: The raw secret value, or ``None``/empty if unset.

    Returns:
        ``"<missing>"`` when unset, otherwise ``"<redacted:XXXXXXXX>"``.
        Identical secrets produce identical tokens within one process, so
        repeated log lines can be correlated without exposing any part of the
        actual value.
    """
    if not value:
        return "<missing>"
    return f"<redacted:{_tag(value)}>"


def redact_text(value: str | None) -> str:
    """Return a log-safe token standing in for a place- or person-identifying string.

    Location and pin names in this app are user-submitted and often
    correspond to undisclosed urbex sites, so they must not appear in logs
    verbatim - nor in any form an attacker could match against a wordlist.

    Args:
        value: The raw text, or ``None``/empty if unset.

    Returns:
        ``"<none>"`` when unset, otherwise ``"<text:XXXXXXXX>"``.
    """
    if not value:
        return "<none>"
    return f"<text:{_tag(value)}>"


def redact_coordinate(value: object) -> str:
    """Return a log-safe token standing in for a latitude/longitude value.

    Coordinates are the lowest-entropy sensitive value this app handles - a
    regional sweep is a few hundred million candidates - so they must never be
    logged in any form derived from the number itself, rounded included.

    Args:
        value: The raw coordinate (numeric or string), or ``None``.

    Returns:
        ``"<none>"`` when unset, otherwise ``"<coord:XXXXXXXX>"``.
    """
    if value is None:
        return "<none>"
    return f"<coord:{_tag(str(value))}>"


def redact_params(params: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of a request-params mapping safe to pass to a logger.

    Fail-closed: a parameter is passed through only if its name is in
    :data:`_PASSTHROUGH_PARAM_NAMES`. Anything unrecognised is redacted, so a
    provider that starts sending a new location-bearing parameter cannot leak
    it just because nobody thought to add it to a blocklist.

    Args:
        params: The raw request parameters (e.g. an API call's query params).

    Returns:
        A new dict with sensitive values replaced by tokens.
    """
    redacted: dict[str, Any] = {}
    for key, value in params.items():
        name = key.casefold()
        if name in _SENSITIVE_PARAM_NAMES:
            redacted[key] = redact_secret(str(value) if value is not None else None)
        elif name in _COORDINATE_PARAM_NAMES:
            redacted[key] = redact_coordinate(value)
        elif name in _PASSTHROUGH_PARAM_NAMES:
            redacted[key] = value
        else:
            redacted[key] = redact_text(str(value)) if value is not None else "<none>"
    return redacted
