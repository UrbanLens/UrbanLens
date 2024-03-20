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
*        Version: 1.0.0                                                                                                *
*        Created: 2024-02-19                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@manlyphotos.com                                                                                 *
*        Copyright (c) 2024 Urban Lens                                                                                 *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2024-02-19     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""

from __future__ import annotations
from typing import Optional
import os

from UrbanLens.environments.types import EnvironmentTypes
from UrbanLens.environments.base import BaseEnvironment
from UrbanLens.environments.local import Local
from UrbanLens.environments.dev import Development
from UrbanLens.environments.test import Testing
from UrbanLens.environments.staging import Staging
from UrbanLens.environments.prod import Production


def select_environment(env_type: Optional[EnvironmentTypes] = None, default: EnvironmentTypes = EnvironmentTypes.LOCAL) -> BaseEnvironment:
    """Selects the environment to use based on the environment type.

    Args:
        env_type (EnvironmentTypes): The environment type to use.

    Returns:
        BaseEnvironment: The environment to use.

    Raises:
        ValueError: If the environment type is unknown.
    """
    if not env_type:
        env_type = os.getenv("URBANLENS_ENVIRONMENT", default=default)

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
