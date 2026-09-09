"""*
ASGI config for urbanlens project.

Routes HTTP through Django's normal ASGI adapter and WebSocket connections
through Channels. Session-authenticated (owner) and token-authenticated
(emergency contact) safety check-in chat both connect here - see
``urbanlens.dashboard.consumers.SafetyCheckinChatConsumer``.

For more information on this file, see
https://docs.djangoproject.com/en/4.2/howto/deployment/asgi/
"""

import logging
import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "urbanlens.UrbanLens.settings")

# Must be constructed before importing anything that touches models/routing -
# it's what populates Django's app registry.
django_asgi_app = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.security.websocket import AllowedHostsOriginValidator  # noqa: E402

from urbanlens.dashboard.checks import websocket_frame_cap_conflict  # noqa: E402
from urbanlens.dashboard.routing import websocket_urlpatterns  # noqa: E402
from urbanlens.dashboard.websocket_auth import ApiKeyAuthMiddlewareStack  # noqa: E402

# Reported here rather than as a registered system check because this is the
# only module that Django imports inside the daphne process, where argv carries
# the frame caps the server was actually started with. Logged rather than
# raised: refusing to start over a misconfigured ceiling would take the whole
# socket tier down, which is worse than the disconnects it warns about.
_frame_cap_conflict = websocket_frame_cap_conflict()
if _frame_cap_conflict:
    logging.getLogger(__name__).error(_frame_cap_conflict)

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        # ApiKeyAuthMiddlewareStack wraps channels.auth.AuthMiddlewareStack: a
        # session always wins, with a ``?key=<PAT or OAuth2 token>`` query-string
        # credential as the fallback for native clients that have no session
        # cookie (see websocket_auth.py).
        "websocket": AllowedHostsOriginValidator(ApiKeyAuthMiddlewareStack(URLRouter(websocket_urlpatterns))),
    },
)
