"""*
ASGI config for urbanlens project.

Routes HTTP through Django's normal ASGI adapter and WebSocket connections
through Channels. Session-authenticated (owner) and token-authenticated
(emergency contact) safety check-in chat both connect here - see
``urbanlens.dashboard.consumers.SafetyCheckinChatConsumer``.

For more information on this file, see
https://docs.djangoproject.com/en/4.2/howto/deployment/asgi/
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "urbanlens.UrbanLens.settings")

# Must be constructed before importing anything that touches models/routing -
# it's what populates Django's app registry.
django_asgi_app = get_asgi_application()

from channels.auth import AuthMiddlewareStack  # noqa: E402
from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.security.websocket import AllowedHostsOriginValidator  # noqa: E402

from urbanlens.dashboard.routing import websocket_urlpatterns  # noqa: E402

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        "websocket": AllowedHostsOriginValidator(AuthMiddlewareStack(URLRouter(websocket_urlpatterns))),
    },
)
