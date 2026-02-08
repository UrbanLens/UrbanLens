"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    factory.py                                                                                           *
*        Path:    /UrbanLens/environments/factory.py                                                                   *
*        Project: urbanlens                                                                                            *
*        Version: 0.0.1                                                                                                *
*        Created: 2024-02-19                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@urbanlens.org                                                                                 *
*        Copyright (c) 2025 Jess Mann                                                                                  *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2024-02-19     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""

from __future__ import annotations
import os

from urbanlens.UrbanLens.environments.types import EnvironmentTypes
from urbanlens.UrbanLens.environments.base import BaseEnvironment
from urbanlens.UrbanLens.environments.local import Local
from urbanlens.UrbanLens.environments.dev import Development
from urbanlens.UrbanLens.environments.test import Testing
from urbanlens.UrbanLens.environments.staging import Staging
from urbanlens.UrbanLens.environments.prod import Production


def select_environment(env_type: EnvironmentTypes | None = None, default: EnvironmentTypes = EnvironmentTypes.LOCAL) -> BaseEnvironment:
    """Selects the environment to use based on the environment type.

    Args:
        env_type (EnvironmentTypes): The environment type to use.

    Returns:
        BaseEnvironment: The environment to use.

    Raises:
        ValueError: If the environment type is unknown.
    """
    if not env_type:
        # Get and validate the environment type from the environment variable
        env_type = EnvironmentTypes(os.getenv("UL_ENVIRONMENT", default=default))

    match env_type:
        case EnvironmentTypes.LOCAL:
            return Local()
        case EnvironmentTypes.DEV:
            return Development()
        case EnvironmentTypes.TEST:
            return Testing()
        case EnvironmentTypes.STAGING:
            return Staging()
        case EnvironmentTypes.PROD:
            return Production()
        case _:
            raise ValueError(f"Unknown environment type: {env_type}")
