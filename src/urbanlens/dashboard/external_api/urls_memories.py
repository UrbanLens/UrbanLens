"""External-API routes for Memories - the retrospective view of a user's history.

Owns the surfaces that re-present a user's own past rather than their current
data: the journal feed (whose first route, ``memories/journal/``, still lives in
``urls.py``), on-this-day and per-period recaps, and the generated memory cards
built from visits and photos.

Memories is derived, not authored. Nothing routed here should be the canonical
home of a record - a memory is a *view* over pins, visits and images that those
domains own - so these endpoints are read-mostly, and the few writes they need
(dismissing a card, opting a period out) belong to the memory, never to the
underlying pin. Keeping that line means a client can drop and rebuild its local
memories cache without risking real data.

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
