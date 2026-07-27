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
    path("push-devices/", views.PushDevicesView.as_view(), name="push_devices"),
    path("push-devices/<uuid:device_uuid>/", views.PushDeviceDetailView.as_view(), name="push_devices.detail"),
    path("friends/", views.FriendsView.as_view(), name="friends"),
    path("friends/<uuid:profile_uuid>/", views.FriendDetailView.as_view(), name="friends.detail"),
    path("friends/<uuid:profile_uuid>/accept/", views.FriendAcceptView.as_view(), name="friends.accept"),
    path("friends/<uuid:profile_uuid>/reject/", views.FriendRejectView.as_view(), name="friends.reject"),
    path("friends/<uuid:profile_uuid>/ignore/", views.FriendIgnoreView.as_view(), name="friends.ignore"),
    path("friends/<uuid:profile_uuid>/block/", views.FriendBlockView.as_view(), name="friends.block"),
    path("friends/<uuid:profile_uuid>/mute/", views.FriendMuteView.as_view(), name="friends.mute"),
    path("friend-invites/", views.FriendInvitesView.as_view(), name="friend_invites"),
    path("notifications/", views.NotificationsView.as_view(), name="notifications"),
    # These two literal paths must stay ahead of the <uuid:notification_uuid>
    # route below - Django matches urlpatterns in order, and although a uuid
    # converter would not in fact match "read-all", keeping the literals first
    # follows this file's existing convention (see the "pins/..." block) and
    # keeps the ordering correct if the converter is ever loosened to <str:>.
    path("notifications/read-all/", views.NotificationsReadAllView.as_view(), name="notifications.read_all"),
    path("notifications/unread-count/", views.NotificationsUnreadCountView.as_view(), name="notifications.unread_count"),
    path("notifications/<uuid:notification_uuid>/", views.NotificationDetailView.as_view(), name="notifications.detail"),
    path("notification-preferences/", views.NotificationDeliveryPreferencesView.as_view(), name="notification_preferences"),
    # Kept after the two literal "profiles/..." sub-resources would-be conflicts:
    # the notes routes are more specific paths under the same slug segment, so
    # they are declared before the bare profile detail route.
    path("profiles/<str:profile_slug>/notes/", views.ProfileNotesView.as_view(), name="profiles.notes"),
    path("profiles/<str:profile_slug>/notes/<uuid:note_uuid>/", views.ProfileNoteDetailView.as_view(), name="profiles.notes.detail"),
    path("profiles/<str:profile_slug>/", views.ProfileDetailView.as_view(), name="profiles.detail"),
    # The machine-readable contract (and a browsable view of it) for exactly
    # this surface - internal endpoints are excluded by
    # schema.preprocess_external_api_only. Served without auth: the schema is
    # the published contract, not user data.
    path("schema/", SpectacularAPIView.as_view(authentication_classes=[], permission_classes=[]), name="schema"),
    path("docs/", SpectacularSwaggerView.as_view(authentication_classes=[], permission_classes=[], url_name="external_api:schema"), name="docs"),
]
