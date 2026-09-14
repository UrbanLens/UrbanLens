"""External-API routes for trip settings and calendar export.

Trip CRUD, members, activities, RSVPs, the map and comments all predate the split and still live in
``urls.py``; every new trip endpoint belongs here instead.
Use ``path()`` (``re_path()`` cannot be ordered and is rejected at import time) and keep every
``name=`` unique across the whole external API.

- **A trip is a shared space.** Unlike a pin, a trip carries other members' identities, their
  comments, and coordinates they may have chosen to reveal only to ...
- **Calendar routes talk to a third party on the request path.** They hold no database transaction
  across those network calls (the underlying service is idempo...
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.urls import path

from urbanlens.dashboard.external_api import views_trips

if TYPE_CHECKING:
    from django.urls.resolvers import URLPattern

#: Routes contributed by this domain.
urlpatterns: list[URLPattern] = [
    path("trips/<slug:trip_slug>/settings/", views_trips.TripSettingsView.as_view(), name="trips.settings"),
    path("trips/<slug:trip_slug>/calendar/", views_trips.TripCalendarExportView.as_view(), name="trips.calendar_export"),
]
