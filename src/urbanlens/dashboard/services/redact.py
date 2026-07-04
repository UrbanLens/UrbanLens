"""Helpers for keeping sensitive values out of application logs.

CodeQL's clear-text-logging query flags any log call fed by a value it
recognises as private (API keys/tokens, geographic coordinates), even when
only a few characters are shown -- a sliced or rounded value is still
"derived from" the original and therefore still tainted. Hashing breaks that
chain, so these helpers hash rather than truncate.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

_SENSITIVE_PARAM_NAMES = frozenset({"key", "api_key", "apikey", "token", "secret", "password", "access_token"})
_COORDINATE_PARAM_NAMES = frozenset({"lat", "lng", "lon", "latitude", "longitude", "latlng", "gscoord"})

_REDACTION_SALT = b"urbanlens:redact:v1"
_PBKDF2_ITERATIONS = 310_000


def _fingerprint(value: str) -> str:
    """Return a short, deterministic PBKDF2-HMAC-SHA256 fingerprint of ``value``."""
    digest = hashlib.pbkdf2_hmac("sha256", value.encode("utf-8"), _REDACTION_SALT, _PBKDF2_ITERATIONS)
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
