"""External-API routes for pin-detail panels.

Owns the pluggable, provider-backed information panels that plugins contribute
to a pin's detail screen (see ``docs/plugins.md`` and ``PanelSource``). The web
UI fetches these lazily over HTMX; a native client needs the same data without
the HTML, which is what this domain exposes.

Panels are the one part of the pin surface that is *not* synchronous. Most are
dispatched to the dedicated ``panel_fetch`` Celery queue, with CPU-heavy ones
opting out via ``PanelSource.queue``, and several call metered third-party APIs.
Endpoints here are therefore job-shaped rather than resource-shaped - ask for a
panel, get either a cached result or a pending marker, poll - and every route
must keep the per-plugin rate limiting and per-call cost tracking intact rather
than reaching around it into the provider gateway.

Wiring: ``urls.py`` concatenates the ``urlpatterns`` below into the flat
``external_api:`` namespace and re-sorts the combined list with
:func:`~urbanlens.dashboard.external_api.urls.order_by_specificity`, so
declaration order inside this module only breaks ties between routes of
identical shape. Use ``path()`` (``re_path()`` cannot be ordered and is
rejected at import time) and keep every ``name=`` unique across the whole
external API.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django.urls.resolvers import URLPattern

#: Routes contributed by this domain. Appended to the flat ``external_api:``
#: namespace by ``urls.py`` - see this module's docstring before adding to it.
urlpatterns: list[URLPattern] = []
