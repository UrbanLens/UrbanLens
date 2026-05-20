"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    __init__.py                                                                                          *
*        Path:    /UrbanLens/environments/__init__.py                                                                  *
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

from urbanlens.UrbanLens.environments.meta import EnvironmentTypes, DebugTypes
from urbanlens.UrbanLens.environments.base import BaseEnvironment

from urbanlens.UrbanLens.environments.local import Local
from urbanlens.UrbanLens.environments.dev import Development
from urbanlens.UrbanLens.environments.test import Testing
from urbanlens.UrbanLens.environments.staging import Staging
from urbanlens.UrbanLens.environments.prod import Production
from urbanlens.UrbanLens.environments.factory import select_environment