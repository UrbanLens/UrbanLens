"""External-API routes for Memories - the retrospective view of a user's history.

Owns the surfaces that re-present a user's own past rather than their current data: the journal feed
(whose first route, ``memories/journal/``, still lives in ``urls.py``), on-this-day and per-period
recaps, and the generated memory cards built from visits and photos.
Nothing routed here should be the canonical home of a record - a memory is a *view* over pins,
visits and images that those domains own - so these endpoints are read-mostly, and the few writes
they need (dismissing a card, opting a period out) belong to the memory, never to the underlying
pin.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.urls import path

from urbanlens.dashboard.external_api import views

if TYPE_CHECKING:
    from django.urls.resolvers import URLPattern

#: Routes contributed by this domain. Appended to the flat ``external_api:``
#: namespace by ``urls.py`` - see this module's docstring before adding to it.
urlpatterns: list[URLPattern] = [
    path("memories/timeline/", views.MemoriesTimelineView.as_view(), name="memories.timeline"),
    path("memories/on-this-day/", views.MemoriesOnThisDayApiView.as_view(), name="memories.on_this_day"),
]
