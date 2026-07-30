"""External-API routes for trip settings and calendar export.

Owns the trip endpoints added after ``urls.py`` was split: the permissions
write path and the Google Calendar export/unexport pair. Trip CRUD, members,
activities, RSVPs, the map and comments all predate the split and still live in
``urls.py``; every new trip endpoint belongs here instead.

Two things to keep in mind when adding to this module:

- **A trip is a shared space.** Unlike a pin, a trip carries other members'
  identities, their comments, and coordinates they may have chosen to reveal
  only to some people. A trip the caller is not a member of must be
  indistinguishable from one that does not exist (404, never 403), and member
  lookups must never leave the trip's own roster. The one deliberate exception
  is the permissions endpoint, which 403s a non-organizer *member* - they have
  already been shown the trip, so the status leaks nothing.
- **Calendar routes talk to a third party on the request path.** They hold no
  database transaction across those network calls (the underlying service is
  idempotent by design) and they carry their own throttle bucket rather than
  the shared write tier, because each call may fan out to one request per trip
  activity.

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

from django.urls import path

from urbanlens.dashboard.external_api import views_trips

if TYPE_CHECKING:
    from django.urls.resolvers import URLPattern

#: Routes contributed by this domain. Appended to the flat ``external_api:``
#: namespace by ``urls.py`` - see this module's docstring before adding to it.
#:
#: The calendar route is named ``trips.calendar_export`` rather than the
#: shorter ``trips.calendar`` that its path would suggest: the site's own
#: urlconf already has a *global* route named ``trips.calendar`` (the month
#: view), and although the namespaces keep them apart for ``reverse()``, two
#: identically named routes pointing at unrelated pages is a trap for anyone
#: grepping for one of them.
urlpatterns: list[URLPattern] = [
    path("trips/<slug:trip_slug>/settings/", views_trips.TripSettingsView.as_view(), name="trips.settings"),
    path("trips/<slug:trip_slug>/calendar/", views_trips.TripCalendarExportView.as_view(), name="trips.calendar_export"),
]
