"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    prod.py                                                                                              *
*        Path:    /UrbanLens/environments/prod.py                                                                      *
*        Project: urbanlens                                                                                            *
*        Version: 1.0.0                                                                                                *
*        Created: 2024-02-19                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@urbanlens.org                                                                                 *
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
from pydantic import validator

from UrbanLens.environments.types import EnvironmentTypes
from UrbanLens.environments.base import BaseEnvironment
from UrbanLens.environments.types import DebugTypes


class Production(BaseEnvironment):
    def __init__(self, **data):
        super().__init__(
            name="Production",
            env_type=EnvironmentTypes.PROD,
            in_network=True,
            is_public=True,
            debug_default=False,
            debug_override=DebugTypes.OVERRIDE_OFF,
            **data,
        )

    @validator("debug_override")
    def debug_override_must_be_off(cls, value):
        """
        Debug mode is not allowed in production
        """
        if value == DebugTypes.OVERRIDE_ON:
            raise ValueError("Debug mode is not allowed in production")
        return value
