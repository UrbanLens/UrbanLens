"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    base.py                                                                                              *
*        Path:    /UrbanLens/environments/base.py                                                                      *
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

import logging
from abc import ABC
from pydantic import BaseModel, Field, validator
from UrbanLens.environments.types import EnvironmentTypes, DebugTypes

logger = logging.getLogger(__name__)

class BaseEnvironment(BaseModel, ABC):
    name: str = Field(default="Unknown Environment")
    env_type: EnvironmentTypes = Field(default=EnvironmentTypes.LOCAL)
    in_network: bool = Field(default=False)
    is_public: bool = Field(default=True)
    debug_default: bool = Field(default=False, description="Default debug setting for this environment if one is not explicitly set")
    debug_override: DebugTypes = Field(default=DebugTypes.DEFAULT, description="Overrides the default debug setting, if set to _ON or _OFF, otherwise uses the default")

    @property
    def debug(self) -> bool:
        if self.debug_override == DebugTypes.OVERRIDE_ON:
            return True
        if self.debug_override == DebugTypes.OVERRIDE_OFF:
            return False
        return self.debug_default

    @validator("env_type", pre=True)
    def validate_env_type(cls, value):
        if isinstance(value, str):
            return EnvironmentTypes(value.lower())
        return value

    @validator("debug_override", pre=True)
    def validate_debug_override(cls, value):
        if isinstance(value, str):
            return DebugTypes(value.lower())
        return value

    def __str__(self):
        return f"Environment: {self.name}"

    def __repr__(self):
        return f"Environment(name={self.name}, env_type={self.env_type})"

    def __eq__(self, other):
        if isinstance(other, EnvironmentTypes):
            return self.env_type == other
        elif isinstance(other, str):
            return self.env_type.value.lower() == other.lower()
        elif isinstance(other, BaseEnvironment):
            return self.env_type == other.env_type
        return NotImplemented
