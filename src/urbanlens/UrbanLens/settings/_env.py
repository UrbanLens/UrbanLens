"""Environment-variable parsing helpers for the Django settings modules.

Kept separate from ``base.py`` so it can be imported and tested without pulling
in the whole settings module's import-time side effects.
"""

from __future__ import annotations

import os

_TRUE_VALUES = frozenset({"1", "true", "t", "yes", "y", "on"})
_FALSE_VALUES = frozenset({"0", "false", "f", "no", "n", "off", ""})


def env_bool(name: str, default: bool) -> bool:
    """Read a boolean environment variable, accepting the spellings people actually use.

    ``base.py`` used to compare against the literal string ``"True"``, so
    ``UL_EMAIL_TLS=true`` - lowercase, and exactly what pydantic-settings accepts
    for the same variable in ``app.py`` - evaluated to False. For the TLS flag that
    silently downgraded SMTP to plaintext while the operator's env file plainly said
    to turn it on. Anything unrecognised falls back to ``default`` rather than
    quietly meaning False, for the same reason.

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


#: Environment names that mean "the one real, shared deployment".
#:
#: Deliberately a closed allow-list rather than a "not one of the dev names"
#: denylist: guards keyed on this decide whether it is safe to send UrbanLens's
#: own data to an upstream service that stores and trains on it, and the
#: expensive direction of a wrong answer is treating a throwaway deployment as
#: production. An unset, misspelled, or newly invented ``UL_ENVIRONMENT`` must
#: therefore land on "not production".
PRODUCTION_ENVIRONMENT_NAMES = frozenset({"production"})


def is_production_environment(name: str | None) -> bool:
    """Whether an environment name denotes the real production deployment.

    Fail-closed by construction - see :data:`PRODUCTION_ENVIRONMENT_NAMES`.
    ``None``, ``""``, ``"prod"``, ``"staging"`` and any unrecognised string
    are all False; only an exact (case- and whitespace-insensitive) match
    against the allow-list is True.

    Args:
        name: The environment name to classify, typically ``UL_ENVIRONMENT``.

    Returns:
        True only for a recognised production environment name.
    """
    if not name:
        return False
    return name.strip().lower() in PRODUCTION_ENVIRONMENT_NAMES
