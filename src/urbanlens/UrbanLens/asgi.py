"""ASGI config: HTTP via Django, WebSocket via Channels.
"""

import logging
import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "urbanlens.UrbanLens.settings")

django_asgi_app = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.security.websocket import AllowedHostsOriginValidator  # noqa: E402

from urbanlens.dashboard.checks import websocket_frame_cap_conflict  # noqa: E402
from urbanlens.dashboard.routing import websocket_urlpatterns  # noqa: E402
from urbanlens.dashboard.websocket_auth import ApiKeyAuthMiddlewareStack  # noqa: E402

# Logged, not raised: refusing to start would take down the socket tier.
_frame_cap_conflict = websocket_frame_cap_conflict()
if _frame_cap_conflict:
    logging.getLogger(__name__).error(_frame_cap_conflict)

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        "websocket": AllowedHostsOriginValidator(ApiKeyAuthMiddlewareStack(URLRouter(websocket_urlpatterns))),
    },
)
