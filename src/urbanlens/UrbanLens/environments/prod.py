from __future__ import annotations

from pydantic import validator

from urbanlens.UrbanLens.environments.base import BaseEnvironment
from urbanlens.UrbanLens.environments.meta import DebugTypes, EnvironmentTypes


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
    @classmethod
    def debug_override_must_be_off(cls, value):
        """
        Debug mode is not allowed in production
        """
        if value == DebugTypes.OVERRIDE_ON:
            raise ValueError("Debug mode is not allowed in production")
        return value
