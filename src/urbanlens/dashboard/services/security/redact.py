"""Helpers for keeping sensitive values out of application logs.
That is the whole point: a hash - even a keyed one - is a function of its input, so anyone who can guess the input can confirm it."""

from __future__ import annotations

from collections import OrderedDict
import re
import secrets
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

#: Parameter names whose values are secrets, compared casefolded with separators dropped (``X-Api-Key`` is ``xapikey``).
SENSITIVE_PARAM_NAMES = frozenset(
    {
        "key",
        "apikey",
        "appid",
        "clientid",
        "token",
        "secret",
        "password",
        "passwd",
        "pwd",
        "sig",
        "signature",
        "auth",
        "authorization",
        "code",
        "credential",
        "credentials",
        "session",
        "sessionid",
    }
)

#: Any name ending in one of these is a secret too: ``access_token``, ``subscription-key``, ``client_secret``, ``X-Amz-Signature``.
_SENSITIVE_NAME_SUFFIXES = ("key", "token", "secret", "password", "signature", "credential")

_NAME_SEPARATORS = re.compile(r"[\W_]+")

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
        "location",
        "ll",
        "latlon",
        "near",
        "proximity",
        "origin",
        "destination",
        "waypoints",
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
    The same value yields the same token for as long as it stays in the cache, so repeated log lines still correlate.

    Args:
        value: The sensitive value to stand in for.

    Returns:
        Eight hex characters of randomness."""
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
        ``"<missing>"`` when unset, otherwise ``"<redacted:XXXXXXXX>"``."""
    if not value:
        return "<missing>"
    return f"<redacted:{_tag(value)}>"


def redact_text(value: str | None) -> str:
    """Return a log-safe token standing in for a place- or person-identifying string.
    Location and pin names in this app are user-submitted and often correspond to undisclosed urbex sites, so they must not appear in logs verbatim - nor in any form an attacker could match against a wordlist.

    Args:
        value: The raw text, or ``None``/empty if unset.

    Returns:
        ``"<none>"`` when unset, otherwise ``"<text:XXXXXXXX>"``."""
    if not value:
        return "<none>"
    return f"<text:{_tag(value)}>"


def redact_coordinate(value: object) -> str:
    """Return a log-safe token standing in for a latitude/longitude value.
    Coordinates are the lowest-entropy sensitive value this app handles - a regional sweep is a few hundred million candidates - so they must never be logged in any form derived from the number itself, rounded included.

    Args:
        value: The raw coordinate (numeric or string), or ``None``.

    Returns:
        ``"<none>"`` when unset, otherwise ``"<coord:XXXXXXXX>"``."""
    if value is None:
        return "<none>"
    return f"<coord:{_tag(str(value))}>"


def redact_params(params: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of a request-params mapping safe to pass to a logger.
    Fail-closed: a parameter is passed through only if its name is in :data:`_PASSTHROUGH_PARAM_NAMES`.

    Args:
        params: The raw request parameters (e.g. an API call's query params).

    Returns:
        A new dict with sensitive values replaced by tokens."""
    redacted: dict[str, Any] = {}
    for key, value in params.items():
        name = key.casefold()
        if is_sensitive_param_name(key):
            redacted[key] = redact_secret(str(value) if value is not None else None)
        elif name in _COORDINATE_PARAM_NAMES:
            redacted[key] = redact_coordinate(value)
        elif name in _PASSTHROUGH_PARAM_NAMES:
            redacted[key] = value
        else:
            redacted[key] = redact_text(str(value)) if value is not None else "<none>"
    return redacted


def is_sensitive_param_name(name: str) -> bool:
    """Whether a request parameter named ``name`` carries a credential.

    Args:
        name: The parameter name, in any case and with any separators.

    Returns:
        True for :data:`SENSITIVE_PARAM_NAMES` and anything ending in a credential word."""
    normalized = _NAME_SEPARATORS.sub("", name.casefold())
    return normalized in SENSITIVE_PARAM_NAMES or normalized.endswith(_SENSITIVE_NAME_SUFFIXES)


_PLAIN_PARAM = re.compile(r"(?P<lead>[?&;])(?P<name>[\w.~\[\]-]+)=(?P<value>[^&;#\s'\"<>]*)")
#: A parameter inside a percent-encoded URL, such as one passed as another URL's parameter.
_ENCODED_PARAM = re.compile(r"(?P<lead>%3F|%26|%3B)(?P<name>[\w.~\[\]-]+)%3D(?P<value>(?:(?!%26|%3B|%23)[^&;#\s'\"<>])*)", re.IGNORECASE)
_USERINFO_PASSWORD = re.compile(r"(?P<lead>(?<![a-z0-9+.-])[a-z][a-z0-9+.-]{0,31}://[^\s/:@?#]*:)(?P<value>[^\s/@?#<>]+)(?=@)", re.IGNORECASE)
_COORDINATE_PAIR = re.compile(r"-?\d{1,3}\.\d+\s*(?:,|%2C)\s*-?\d{1,3}\.\d+", re.IGNORECASE)


def _redact_param(match: re.Match[str]) -> str:
    value = match["value"]
    if not value:
        return match[0]
    if is_sensitive_param_name(match["name"]):
        token = redact_secret(value)
    elif match["name"].casefold() in _COORDINATE_PARAM_NAMES or _COORDINATE_PAIR.search(value):
        token = redact_coordinate(value)
    else:
        return match[0]
    return match.string[match.start() : match.start("value")] + token


def _redact_password(match: re.Match[str]) -> str:
    return match["lead"] + redact_secret(match["value"])


def redact_urls(text: str) -> str:
    """Return ``text`` with the credentials and coordinates in every URL it contains replaced by tokens.

    Covers query parameters (plain and percent-encoded) and ``user:password@`` userinfo. Everything else in the text,
    other parameters included, is left as it was.

    Args:
        text: A log message, exception message or traceback.

    Returns:
        The text, with each secret parameter value a :func:`redact_secret` token and each location a
        :func:`redact_coordinate` token."""
    if "=" not in text and "%" not in text and "@" not in text:
        return text
    text = _PLAIN_PARAM.sub(_redact_param, text)
    text = _ENCODED_PARAM.sub(_redact_param, text)
    return _USERINFO_PASSWORD.sub(_redact_password, text) if "://" in text else text
