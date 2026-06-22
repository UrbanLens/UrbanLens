from __future__ import annotations

from urbanlens.UrbanLens.environments.base import BaseEnvironment
from urbanlens.UrbanLens.environments.meta import EnvironmentTypes


class Staging(BaseEnvironment):
    def __init__(self, **data):
        super().__init__(
            name="staging",
            env_type=EnvironmentTypes.STAGING,
            in_network=True,
            is_public=True,
            debug_default=False,
            **data,
        )
