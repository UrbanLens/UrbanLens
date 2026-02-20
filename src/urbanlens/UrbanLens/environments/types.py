"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    types.py                                                                                             *
*        Path:    /UrbanLens/environments/types.py                                                                     *
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

from enum import Enum, StrEnum


class EnvironmentTypes(StrEnum):
    DEV = "dev"
    TEST = "test"
    PROD = "prod"
    STAGING = "staging"
    LOCAL = "local"


class DebugTypes(StrEnum):
    OVERRIDE_ON = "override_on"
    OVERRIDE_OFF = "override_off"
    DEFAULT = "default"
