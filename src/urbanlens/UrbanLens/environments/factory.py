from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.UrbanLens.environments.dev import Development
from urbanlens.UrbanLens.environments.local import Local
from urbanlens.UrbanLens.environments.meta import EnvironmentTypes, environment_from_env
from urbanlens.UrbanLens.environments.prod import Production
from urbanlens.UrbanLens.environments.staging import Staging
from urbanlens.UrbanLens.environments.test import Testing

if TYPE_CHECKING:
    from urbanlens.UrbanLens.environments.base import BaseEnvironment


def select_environment(env_type: str | EnvironmentTypes | None = None) -> BaseEnvironment:
    """Select the environment for the given type.

    Args:
        env_type: Environment type (strings coerced); empty resolves ``UL_ENVIRONMENT`` via ``environment_from_env``.

    Returns:
        The matching environment instance.

    Raises:
        ValueError: If the environment type is unknown.
    """
    if isinstance(env_type, str):
        env_type = EnvironmentTypes(env_type)

    if not env_type:
        env_type = environment_from_env()

    match env_type:
        case EnvironmentTypes.LOCAL:
            return Local()
        case EnvironmentTypes.DEVELOPMENT:
            return Development()
        case EnvironmentTypes.TESTING:
            return Testing()
        case EnvironmentTypes.STAGING:
            return Staging()
        case EnvironmentTypes.PRODUCTION:
            return Production()
        case _:
            raise ValueError(f"Unknown environment type: {env_type}")
