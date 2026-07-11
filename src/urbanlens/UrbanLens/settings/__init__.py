import os

if os.getenv("UL_ENVIRONMENT", "local").lower() == "local":
    from urbanlens.UrbanLens.settings.local import *  # noqa: F403
else:
    from urbanlens.UrbanLens.settings.base import *  # noqa: F403
