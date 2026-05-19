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

from urbanlens.Urbanlens.environments.meta import EnvironmentTypes, DebugTypes
from urbanlens.Urbanlens.environments.base import BaseEnvironment

from urbanlens.Urbanlens.environments.local import Local
from urbanlens.Urbanlens.environments.dev import Development
from urbanlens.Urbanlens.environments.test import Testing
from urbanlens.Urbanlens.environments.staging import Staging
from urbanlens.Urbanlens.environments.prod import Production
from urbanlens.Urbanlens.environments.factory import select_environment