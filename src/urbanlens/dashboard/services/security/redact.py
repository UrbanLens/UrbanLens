"""Helpers for keeping sensitive values out of application logs.

CodeQL's clear-text-logging query flags any log call fed by a value it
recognises as private (API keys/tokens, geographic coordinates), even when
only a few characters are shown -- a sliced or rounded value is still
"derived from" the original and therefore still tainted. Hashing breaks that
chain, so these helpers hash rather than truncate.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

_SENSITIVE_PARAM_NAMES = frozenset({"key", "api_key", "apikey", "token", "secret", "password", "access_token"})
_COORDINATE_PARAM_NAMES = frozenset({"lat", "lng", "lon", "latitude", "longitude", "latlng", "gscoord", "loc", "coordinates"})

_REDACTION_SALT = b"urbanlens:redact:v1"


def _fingerprint(value: str) -> str:
    """Return a short, deterministic HMAC-SHA256 fingerprint of ``value``.

    Uses a fast keyed HMAC rather than a deliberately-slow password hash (PBKDF2/bcrypt/etc) -
    the threat model here is "don't let a log line leak the raw value," not "resist offline
    brute-forcing of a low-entropy secret," so there's no need to pay ~200ms of CPU per call for
    password-hashing-grade iteration counts. A keyed HMAC still makes the fingerprint
    non-reversible and non-forgeable without the key, which is all this needs.
    """
    # CodeQL alert #463 (py/weak-sensitive-data-hashing): dismissed as a false positive. This is a
    # keyed HMAC used for log-correlation fingerprints, not password storage/verification -- see
    # the module and function docstrings above. The legacy "# lgtm[...]" syntax this line used to
    # carry is not honored by this repo's default code-scanning setup, so it never actually
    # suppressed anything; dismissing via the GitHub API is the durable fix.
    digest = hmac.new(_REDACTION_SALT, value.encode("utf-8"), hashlib.sha256).digest()
    return digest.hex()[:8]


def redact_secret(value: str | None) -> str:
    """Return a log-safe fingerprint for an API key, token, or secret.

    Args:
        value: The raw secret value, or ``None``/empty if unset.

    Returns:
        ``"<missing>"`` when unset, otherwise ``"<redacted:XXXXXXXX>"`` where
        the suffix is a short SHA-256 fingerprint. Identical secrets produce
        identical fingerprints, so repeated log lines can still be
        correlated without exposing any part of the actual value.
    """
    if not value:
        return "<missing>"
    return f"<redacted:{_fingerprint(value)}>"


def redact_text(value: str | None) -> str:
    """Return a log-safe fingerprint for a free-text field that may identify a place or person.

    Location and pin names in this app are user-submitted and often
    correspond to undisclosed urbex sites, so they should not appear in logs
    verbatim.

    Args:
        value: The raw text, or ``None``/empty if unset.

    Returns:
        ``"<none>"`` when unset, otherwise ``"<text:XXXXXXXX>"`` where the
        suffix is a short SHA-256 fingerprint of the value.
    """
    if not value:
        return "<none>"
    return f"<text:{_fingerprint(value)}>"


def redact_coordinate(value: object) -> str:
    """Return a log-safe fingerprint for a latitude/longitude value.

    Locations in this app are user-submitted and often meant to stay
    undisclosed, so exact coordinates should not appear in logs.

    Args:
        value: The raw coordinate (numeric or string), or ``None``.

    Returns:
        ``"<none>"`` when unset, otherwise ``"<coord:XXXXXXXX>"`` where the
        suffix is a short SHA-256 fingerprint of the value's string form.
    """
    if value is None:
        return "<none>"
    return f"<coord:{_fingerprint(str(value))}>"


def redact_params(params: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of a request-params mapping safe to pass to a logger.

    Keys matching known secret or coordinate parameter names (``key``,
    ``token``, ``lat``, ``latlng``, etc.) are replaced with fingerprints;
    everything else is passed through unchanged.

    Args:
        params: The raw request parameters (e.g. an API call's query params).

    Returns:
        A new dict with sensitive values redacted.
    """
    redacted: dict[str, Any] = {}
    for key, value in params.items():
        name = key.casefold()
        if name in _SENSITIVE_PARAM_NAMES:
            redacted[key] = redact_secret(str(value) if value is not None else None)
        elif name in _COORDINATE_PARAM_NAMES:
            redacted[key] = redact_coordinate(value)
        else:
            redacted[key] = value
    return redacted
