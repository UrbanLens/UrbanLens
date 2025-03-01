"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    local.py                                                                                             *
*        Path:    /UrbanLens/environments/local.py                                                                     *
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


class Local(BaseEnvironment):
    def __init__(self, **data):
        super().__init__(
            name="local",
            env_type=EnvironmentTypes.LOCAL,
            in_network=False,
            is_public=False,
            debug_default=True,
            **data,
        )
