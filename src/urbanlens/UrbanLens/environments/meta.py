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
    """Resolve ``UL_ENVIRONMENT``, treating unset or blank as production.

    Compose, the entrypoint and the Dockerfile all default to production, so a process started without the
    variable is assumed to be a deployment and must be configured like one.

    Args:
        environ: Environment to read; defaults to this process's own.

    Returns:
        The named environment.

    Raises:
        ImproperlyConfigured: When the name is not a known environment, rather than guessing which one a typo meant.
    """
    raw = (os.environ if environ is None else environ).get("UL_ENVIRONMENT", "").strip().lower()
    if not raw:
        return EnvironmentTypes.PRODUCTION
    try:
        return EnvironmentTypes(raw)
    except ValueError:
        known = ", ".join(sorted(EnvironmentTypes))
        raise ImproperlyConfigured(f"UL_ENVIRONMENT={raw!r} is not a known environment; use one of: {known}.") from None
