"""External-API routes for the partner side of safety check-ins.

Acknowledging that they have seen it, escalating when a check-in goes overdue, reading whatever live
position the explorer chose to share, and managing their own standing invitations and defaults as a
partner rather than as an owner.
Use ``path()`` (``re_path()`` cannot be ordered and is rejected at import time) and keep every
``name=`` unique across the whole external API.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.urls import path

from urbanlens.dashboard.external_api import views_safety_chat, views_safety_location

if TYPE_CHECKING:
    from django.urls.resolvers import URLPattern

#: Routes contributed by this domain. Collapsing them would put both behind a query parameter, where getting the
#: filter wrong silently widens a read instead of failing to resolve a route.
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
