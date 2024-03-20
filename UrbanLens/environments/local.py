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

from UrbanLens.environments.types import EnvironmentTypes
from UrbanLens.environments.base import BaseEnvironment


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
