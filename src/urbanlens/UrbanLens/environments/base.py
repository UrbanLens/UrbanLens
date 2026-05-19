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

from abc import ABC
import logging

from pydantic import BaseModel, Field, field_validator

from urbanlens.UrbanLens.environments.meta import DebugTypes, EnvironmentTypes

logger = logging.getLogger(__name__)


class BaseEnvironment(BaseModel, ABC):
    name: str = Field(default="Unknown Environment")
    env_type: EnvironmentTypes = Field(default=EnvironmentTypes.LOCAL)
    in_network: bool = Field(default=False)
    is_public: bool = Field(default=True)
    debug_default: bool = Field(
        default=False,
        description="Default debug setting for this environment if one is not explicitly set",
    )
    debug_override: DebugTypes = Field(
        default=DebugTypes.DEFAULT,
        description="Overrides the default debug setting, if set to _ON or _OFF, otherwise uses the default",
    )

    @property
    def debug(self) -> bool:
        if self.debug_override == DebugTypes.OVERRIDE_ON:
            return True
        if self.debug_override == DebugTypes.OVERRIDE_OFF:
            return False
        return self.debug_default

    @field_validator("env_type", mode="before")
    @classmethod
    def validate_env_type(cls, value: str | EnvironmentTypes) -> EnvironmentTypes:
        if not isinstance(value, EnvironmentTypes):
            return EnvironmentTypes(value.lower())
        return value

    @field_validator("debug_override", mode="before")
    @classmethod
    def validate_debug_override(cls, value: str | DebugTypes) -> DebugTypes:
        if not isinstance(value, DebugTypes):
            return DebugTypes(value.lower())
        return value

    def __str__(self):
        return f"Environment: {self.name}"

    def __repr__(self):
        return f"Environment(name={self.name}, env_type={self.env_type})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, EnvironmentTypes):
            return self.env_type == other
        if isinstance(other, str):
            return self.env_type.value.lower() == other.lower()
        if isinstance(other, BaseEnvironment):
            return self.env_type == other.env_type
        return NotImplemented
