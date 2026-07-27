"""URL routes for the external API, mounted at ``dashboard/api/external/v1/``.

Versioned and namespaced separately from the internal REST surface
(``dashboard/rest/``, see ``dashboard/urls.py``) because this one has a
public consumer contract - a third-party application holding a user's API
key - that the internal API doesn't.
"""

from __future__ import annotations

from django.urls import path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from urbanlens.dashboard.external_api import views

app_name = "external_api"

urlpatterns = [
    path("whoami/", views.WhoAmIView.as_view(), name="whoami"),
    path("auth/session/", views.AuthSessionView.as_view(), name="auth.session"),
    path("settings/", views.AccountSettingsView.as_view(), name="settings"),
    path("pins/", views.PinsView.as_view(), name="pins"),
    path("pins/deleted/", views.PinTombstonesView.as_view(), name="pins.deleted"),
    # Must stay after the two literal "pins/..." paths above - Django matches
    # urlpatterns in order, and this generic slug segment would otherwise
    # swallow both of them.
    path("pins/<str:pin_slug>/", views.PinDetailView.as_view(), name="pins.detail"),
    path("pin-suggestions/", views.PinSuggestionsView.as_view(), name="pin_suggestions"),
    # Trips. Every literal sub-path below sits *after* the trip slug segment
    # rather than beside it, so unlike "pins/..." above there is nothing here
    # for a generic slug to swallow - "trips/" and "trips/<slug>/" cannot
    # collide, and each deeper segment is a distinct literal.
    path("trips/", views.TripsView.as_view(), name="trips"),
    path("trips/<slug:trip_slug>/", views.TripDetailView.as_view(), name="trips.detail"),
    path("trips/<slug:trip_slug>/map/", views.TripMapView.as_view(), name="trips.map"),
    path("trips/<slug:trip_slug>/join/", views.TripJoinView.as_view(), name="trips.join"),
    path("trips/<slug:trip_slug>/leave/", views.TripLeaveView.as_view(), name="trips.leave"),
    path("trips/<slug:trip_slug>/rsvp/", views.TripRsvpView.as_view(), name="trips.rsvp"),
    path("trips/<slug:trip_slug>/calendar-sync/", views.TripCalendarSyncView.as_view(), name="trips.calendar_sync"),
    path("trips/<slug:trip_slug>/members/", views.TripMembersView.as_view(), name="trips.members"),
    path("trips/<slug:trip_slug>/members/<slug:member_slug>/", views.TripMemberDetailView.as_view(), name="trips.members.detail"),
    path("trips/<slug:trip_slug>/activities/", views.TripActivitiesView.as_view(), name="trips.activities"),
    path("trips/<slug:trip_slug>/activities/<int:activity_id>/", views.TripActivityDetailView.as_view(), name="trips.activities.detail"),
    path("trips/<slug:trip_slug>/activities/<int:activity_id>/position/", views.TripActivityPositionView.as_view(), name="trips.activities.position"),
    path("trips/<slug:trip_slug>/activities/<int:activity_id>/vote/", views.TripActivityVoteView.as_view(), name="trips.activities.vote"),
    path("trips/<slug:trip_slug>/activities/<int:activity_id>/status/", views.TripActivityStatusView.as_view(), name="trips.activities.status"),
    path("trips/<slug:trip_slug>/activities/<int:activity_id>/rsvp/", views.TripActivityRsvpView.as_view(), name="trips.activities.rsvp"),
    path("trips/<slug:trip_slug>/comments/", views.TripCommentsView.as_view(), name="trips.comments"),
    path("trips/<slug:trip_slug>/comments/<int:comment_id>/", views.TripCommentDetailView.as_view(), name="trips.comments.detail"),
    path("trips/<slug:trip_slug>/comments/<int:comment_id>/reactions/", views.TripCommentReactionsView.as_view(), name="trips.comments.reactions"),
    path("push-devices/", views.PushDevicesView.as_view(), name="push_devices"),
    path("push-devices/<uuid:device_uuid>/", views.PushDeviceDetailView.as_view(), name="push_devices.detail"),
    # The machine-readable contract (and a browsable view of it) for exactly
    # this surface - internal endpoints are excluded by
    # schema.preprocess_external_api_only. Served without auth: the schema is
    # the published contract, not user data.
    path("schema/", SpectacularAPIView.as_view(authentication_classes=[], permission_classes=[]), name="schema"),
    path("docs/", SpectacularSwaggerView.as_view(authentication_classes=[], permission_classes=[], url_name="external_api:schema"), name="docs"),
]
