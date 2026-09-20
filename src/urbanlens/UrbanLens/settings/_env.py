"""Env parsing helpers for Django settings modules."""

from __future__ import annotations

import os
import sys

_TRUE_VALUES = frozenset({"1", "true", "t", "yes", "y", "on"})
_FALSE_VALUES = frozenset({"0", "false", "f", "no", "n", "off", ""})


def env_bool(name: str, default: bool) -> bool:
    """Read a boolean env var; fall back to default when unset or unrecognised.

    Args:
        name: The environment variable to read.
        default: Value to use when the variable is unset or unrecognised.

    Returns:
        The parsed boolean.
    """
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
    """Return True only for a recognised production environment name.

    Args:
        name: The environment name to classify, typically ``UL_ENVIRONMENT``.

    Returns:
        True only for a recognised production environment name.
    """
    if not name:
        return False
    return name.strip().lower() in PRODUCTION_ENVIRONMENT_NAMES


def persistent_connection_seconds(default: int = 0, argv: list[str] | None = None) -> int:
    """Read ``UL_DB_CONN_MAX_AGE``, refusing to keep connections a server cannot hand back.

    A connection kept past the response is only reclaimed by the thread that opened it asking for
    one again. ``runserver`` gives each HTTP connection its own thread and drops it afterwards, so
    the socket stays idle on the server until it ages out of ``pg_stat_activity`` on its own -
    which, at a viewport of tiles per map load, reaches the role's connection limit in a couple of
    loads. Bounded pools (gunicorn's ``gthread``, Channels' single loop) do reuse theirs.

    Args:
        default: Value to use when the variable is unset or unreadable.
        argv: Command line to classify; defaults to this process's own.

    Returns:
        Seconds to keep a connection open between requests; 0 to close it with the response.
    """
    if "runserver" in (sys.argv if argv is None else argv):
        return 0
    raw = os.getenv("UL_DB_CONN_MAX_AGE")
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default
