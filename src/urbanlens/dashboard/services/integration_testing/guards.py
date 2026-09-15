"""The two locks an integration command checks before it runs against production."""

from __future__ import annotations

import os

from urbanlens.dashboard.services.integration_testing import INTEGRATION_OVERRIDE_ENV_VAR
from urbanlens.UrbanLens.environments.meta import EnvironmentTypes
from urbanlens.UrbanLens.settings.app import settings as app_settings


def is_production() -> bool:
    """Whether ``UL_ENVIRONMENT`` is production."""
    return str(app_settings.environment_name) == EnvironmentTypes.PRODUCTION


def production_unlocked(*, force: bool) -> bool:
    """Whether both locks are open: ``--force``, and the override variable set true.

    Each covers a different mistake: ``--force`` a command typed in the wrong terminal, the variable a script that has
    always carried ``--force`` being pointed somewhere new.

    Args:
        force: Whether ``--force`` was passed.

    Returns:
        Whether a command may run against production.
    """
    return force and os.environ.get(INTEGRATION_OVERRIDE_ENV_VAR, "").strip().lower() in {"1", "true", "yes", "on"}
