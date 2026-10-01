"""ASGI config: HTTP via Django, WebSocket via Channels."""

import logging
import os

from urbanlens.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "urbanlens.UrbanLens.settings")

django_asgi_app = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.security.websocket import AllowedHostsOriginValidator  # noqa: E402

from urbanlens.core.warmup import warm_urlconf  # noqa: E402
from urbanlens.dashboard.checks import websocket_frame_cap_conflict  # noqa: E402
from urbanlens.dashboard.routing import websocket_urlpatterns  # noqa: E402
from urbanlens.dashboard.websocket_auth import ApiKeyAuthMiddlewareStack  # noqa: E402

# Logged, not raised: refusing to start would take down the socket tier.
_frame_cap_conflict = websocket_frame_cap_conflict()
if _frame_cap_conflict:
    logging.getLogger(__name__).error(_frame_cap_conflict)


def _warm_urlconf() -> None:
    """Import the HTTP URLconf at boot rather than on the first request.

    The imports above cover the *websocket* patterns only, so the HTTP URLconf -
    which reaches every controller, and through them GeoPandas/Shapely - stays
    unloaded until something asks for an HTTP route, along with the reverse
    table every ``{% url %}`` needs.

    daphne is a single process with ``cpus: 1``, and that work holds the GIL, so
    paying it on request one stalls the event loop serving every WebSocket on
    the site - after the container has already reported itself healthy. gunicorn
    solves this in ``post_worker_init`` (gunicorn.conf.py); daphne loads no
    gunicorn config, so it needs its own call to the same helper.
    """
    try:
        patterns, reversible = warm_urlconf()
        logging.getLogger(__name__).info("URLconf warmed: %d root patterns, %d reversible names", patterns, reversible)
    except Exception:
        # A warm-up is an optimisation; the request path will raise the same
        # error somewhere it can be handled.
        logging.getLogger(__name__).exception("URLconf warm-up failed; continuing without it")


_warm_urlconf()

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        "websocket": AllowedHostsOriginValidator(ApiKeyAuthMiddlewareStack(URLRouter(websocket_urlpatterns))),
    },
)
