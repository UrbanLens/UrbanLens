from __future__ import annotations

from urbanlens.UrbanLens.environments.base import BaseEnvironment
from urbanlens.UrbanLens.environments.meta import DebugTypes, EnvironmentTypes


class Development(BaseEnvironment):
    def __init__(self, debug_override: DebugTypes = DebugTypes.DEFAULT):
        super().__init__(
            name="development",
            env_type=EnvironmentTypes.DEV,
            in_network=True,
            is_public=False,
            debug_default=True,
            debug_override=debug_override,
        )
