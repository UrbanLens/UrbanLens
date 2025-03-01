"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    dev.py                                                                                               *
*        Path:    /UrbanLens/environments/dev.py                                                                       *
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

from urbanlens.UrbanLens.environments.types import EnvironmentTypes
from urbanlens.UrbanLens.environments.base import BaseEnvironment


class Development(BaseEnvironment):
    def __init__(self, **data):
        super().__init__(
            name="development",
            env_type=EnvironmentTypes.DEV,
            in_network=True,
            is_public=False,
            debug_default=True,
            **data,
        )
