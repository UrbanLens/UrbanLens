from urbanlens.UrbanLens.settings.base import *  # noqa: F403
from urbanlens.UrbanLens.settings.base import _is_ephemeral

# After base, which has loaded .env: the environment is only known once it has.
if _is_ephemeral:
    from urbanlens.UrbanLens.settings.local import *  # noqa: F403
