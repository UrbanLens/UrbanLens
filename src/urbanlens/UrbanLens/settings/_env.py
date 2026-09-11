"""Env parsing helpers for Django settings modules."""

from __future__ import annotations

import os

_TRUE_VALUES = frozenset({"1", "true", "t", "yes", "y", "on"})
_FALSE_VALUES = frozenset({"0", "false", "f", "no", "n", "off", ""})


def env_bool(name: str, default: bool) -> bool:
    """Read a boolean env var; fall back to default when unset or unrecognised."""
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    return default


#: Environment names meaning the real shared deployment. Fail-closed allow-list.
PRODUCTION_ENVIRONMENT_NAMES = frozenset({"production"})


def is_production_environment(name: str | None) -> bool:
    """Return True only for a recognised production environment name."""
    if not name:
        return False
    return name.strip().lower() in PRODUCTION_ENVIRONMENT_NAMES
