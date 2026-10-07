from __future__ import annotations

from enum import StrEnum
import os
from typing import TYPE_CHECKING

from django.core.exceptions import ImproperlyConfigured

if TYPE_CHECKING:
    from collections.abc import Mapping


class EnvironmentTypes(StrEnum):
    DEVELOPMENT = "development"
    TESTING = "testing"
    PRODUCTION = "production"
    STAGING = "staging"
    LOCAL = "local"


class DebugTypes(StrEnum):
    OVERRIDE_ON = "override_on"
    OVERRIDE_OFF = "override_off"
    DEFAULT = "default"


#: Environments holding no durable shared data, where a setting a deployment must provide may fall back to a
#: local default instead of refusing to start.
EPHEMERAL_ENVIRONMENTS = frozenset({EnvironmentTypes.LOCAL, EnvironmentTypes.DEVELOPMENT, EnvironmentTypes.TESTING})


def environment_from_env(environ: Mapping[str, str] | None = None) -> EnvironmentTypes:
    """Resolve ``UL_ENVIRONMENT``, refusing a process that does not say which environment it is.

    Unset or blank used to mean production, so a deployment that lost the variable spent production's budgets and
    sent real mail. Since Jess's ruling of 2026-10-07 it refuses to start instead, as an unknown name always has.
    A test run that names none is the exception, and ``settings.base`` decides it before calling this.

    Args:
        environ: Environment to read; defaults to this process's own.

    Returns:
        The named environment.

    Raises:
        ImproperlyConfigured: When the name is unset, blank or not a known environment, rather than guessing.
    """
    raw = (os.environ if environ is None else environ).get("UL_ENVIRONMENT", "").strip().lower()
    try:
        return EnvironmentTypes(raw)
    except ValueError:
        known = ", ".join(sorted(EnvironmentTypes))
        problem = "UL_ENVIRONMENT is not set" if not raw else f"UL_ENVIRONMENT={raw!r} is not a known environment"
        raise ImproperlyConfigured(f"{problem}; use one of: {known}.") from None
