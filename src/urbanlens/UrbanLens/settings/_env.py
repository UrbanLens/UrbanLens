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
