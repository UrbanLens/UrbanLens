"""Env parsing helpers for Django settings modules."""

from __future__ import annotations

import os
import sys

from django.core.exceptions import ImproperlyConfigured

from urbanlens.UrbanLens.environments.meta import EPHEMERAL_ENVIRONMENTS

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


def running_under_pytest(argv: list[str] | None = None) -> bool:
    """Whether this process is a pytest run, which is never a deployment whatever ``UL_ENVIRONMENT`` says.

    Args:
        argv: Command line to classify; defaults to this process's own.

    Returns:
        True when any argument names pytest.
    """
    return any("pytest" in arg for arg in (sys.argv if argv is None else argv))


def deployment_settings_required(environment: str, argv: list[str] | None = None) -> bool:
    """Whether a setting a deployment must provide has to be present rather than defaulted.

    Args:
        environment: The resolved ``UL_ENVIRONMENT``.
        argv: Command line to classify; defaults to this process's own.

    Returns:
        True outside local, development and testing, except under pytest.
    """
    return environment not in EPHEMERAL_ENVIRONMENTS and not running_under_pytest(argv)


def require_deployment_setting(name: str, value: str | None, *, environment: str, fallback: str, reason: str, argv: list[str] | None = None) -> str:
    """Return ``value``, or ``fallback`` where a missing deployment setting is harmless, or refuse to start.

    Django system checks do not run under daphne, gunicorn or celery, so the refusal happens at import.

    Args:
        name: What the operator sets, for the message.
        value: The configured value, if any.
        environment: The resolved ``UL_ENVIRONMENT``.
        fallback: What local, development and testing use when it is unset.
        reason: What goes wrong without it, for the message.
        argv: Command line to classify; defaults to this process's own.

    Returns:
        The stripped value, or the fallback.

    Raises:
        ImproperlyConfigured: When it is unset in a deployment.
    """
    value = (value or "").strip()
    if value:
        return value
    if not deployment_settings_required(environment, argv):
        return fallback
    raise ImproperlyConfigured(f"{name} must be set when UL_ENVIRONMENT is '{environment}'. {reason}")


_LOOPBACK_HOSTS = frozenset({"localhost", "::1"})


def is_loopback_host(host: str | None) -> bool:
    """Whether ``host`` only ever names the machine it is read on.

    Args:
        host: A hostname or IP literal, unbracketed.

    Returns:
        True for localhost, ``*.localhost`` and loopback addresses.
    """
    host = (host or "").strip().lower().rstrip(".")
    return host in _LOOPBACK_HOSTS or host.endswith(".localhost") or host.startswith("127.")


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


#: Executions of one statement on one connection before psycopg prepares it server-side. Django's
#: own default is ``None`` - prepared statements off - so that a transaction-pooling proxy in front
#: of Postgres keeps working. There is none here. See X27.
DEFAULT_PREPARE_THRESHOLD = 5


def prepare_threshold(default: int | None = DEFAULT_PREPARE_THRESHOLD) -> int | None:
    """Read ``UL_DB_PREPARE_THRESHOLD``, where blank or ``none`` means "never prepare".

    Args:
        default: Value to use when the variable is unset or unreadable.

    Returns:
        Executions before a statement is prepared, or None to leave every one re-planned - which
        is what a deployment that puts a transaction-pooling proxy in front of Postgres needs.
    """
    raw = os.getenv("UL_DB_PREPARE_THRESHOLD")
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"", "none", "off"}:
        return None
    try:
        return max(0, int(normalized))
    except ValueError:
        return default
