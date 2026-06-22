from __future__ import annotations

from urbanlens.UrbanLens.environments.base import BaseEnvironment
from urbanlens.UrbanLens.environments.meta import EnvironmentTypes


class Testing(BaseEnvironment):
    def __init__(self, **data):
        super().__init__(
            name="testing",
            env_type=EnvironmentTypes.TEST,
            in_network=True,
            is_public=False,
            debug_default=True,
            **data,
        )
