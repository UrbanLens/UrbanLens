"""External-API routes for the partner side of safety check-ins.

``urls.py`` routes the *explorer's* half of a check-in: creating one, marking
yourself safe, cancelling, and attaching photos or maps to it. This module owns
the other half - what the people watching a check-in can do. Acknowledging that
they have seen it, escalating when a check-in goes overdue, reading whatever
live position the explorer chose to share, and managing their own standing
invitations and defaults as a partner rather than as an owner.

The split matters for authorization, not just tidiness. Explorer endpoints ask
"is this your check-in"; partner endpoints ask "were you named on it", which is
a different query against a different relation, and conflating the two is how a
partner ends up able to edit a check-in or an ex-partner keeps reading someone's
location after being removed. Safety data is also the most sensitive thing this
API carries: a check-in reveals where a person physically is, right now, so
routes here should expose the minimum a watcher needs and nothing more.

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

from urbanlens.dashboard.external_api import views_safety_chat, views_safety_location

if TYPE_CHECKING:
    from django.urls.resolvers import URLPattern

#: Routes contributed by this domain. Appended to the flat ``external_api:``
#: namespace by ``urls.py`` - see this module's docstring before adding to it.
#:
#: Two addressing schemes appear below, and the split is deliberate. The chat
#: route sits under ``safety/checkins/<slug>/`` alongside the owner's own
#: endpoints, because its caller may well *be* the owner and a slug is their
#: natural handle. Everything else is mounted under ``safety/partner-*`` and
#: keyed by **uuid**: a check-in's slug is unique per owner rather than
#: globally, so it cannot name someone else's check-in, and a partner-facing
#: route that accepted one would resolve ambiguously for anyone watching two
#: people whose check-in titles happen to slugify alike.
#:
#: ``partner-invites`` and ``partner-checkins`` are separate collections rather
#: than one list with a status filter, because they are not the same resource:
#: an invite row is answerable (accept/decline) and exposes almost nothing about
#: the check-in, while a partnered check-in is readable in full. Collapsing them
#: would put both behind a query parameter, where getting the filter wrong
#: silently widens a read instead of failing to resolve a route.
urlpatterns: list[URLPattern] = [
    path("safety/checkins/<str:checkin_slug>/messages/", views_safety_chat.SafetyCheckinMessagesView.as_view(), name="safety.checkins.messages"),
    path("safety/checkins/<str:checkin_slug>/location/", views_safety_location.SafetyCheckinLocationView.as_view(), name="safety.checkins.location"),
    path("safety/partner-invites/", views_safety_chat.SafetyPartnerInvitesView.as_view(), name="safety.partner_invites"),
    path("safety/partner-invites/<uuid:checkin_uuid>/accept/", views_safety_chat.SafetyPartnerInviteAcceptView.as_view(), name="safety.partner_invites.accept"),
    path("safety/partner-invites/<uuid:checkin_uuid>/decline/", views_safety_chat.SafetyPartnerInviteDeclineView.as_view(), name="safety.partner_invites.decline"),
    path("safety/partner-checkins/", views_safety_chat.SafetyPartnerCheckinsView.as_view(), name="safety.partner_checkins"),
    path("safety/partner-checkins/<uuid:checkin_uuid>/mark-safe/", views_safety_chat.SafetyPartnerMarkSafeView.as_view(), name="safety.partner_checkins.mark_safe"),
    path("safety/partner-checkins/<uuid:checkin_uuid>/", views_safety_chat.SafetyPartnerCheckinDetailView.as_view(), name="safety.partner_checkins.detail"),
]
