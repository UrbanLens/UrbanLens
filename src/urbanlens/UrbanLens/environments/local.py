from __future__ import annotations

from urbanlens.UrbanLens.environments.base import BaseEnvironment
from urbanlens.UrbanLens.environments.meta import EnvironmentTypes


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
