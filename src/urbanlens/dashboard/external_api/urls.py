"""URL routes for the external API, mounted at ``dashboard/api/external/v1/``.

Versioned and namespaced separately from the internal REST surface
(``dashboard/rest/``, see ``dashboard/urls.py``) because this one has a
public consumer contract - a third-party application holding a user's API
key - that the internal API doesn't.
"""

from __future__ import annotations

from django.urls import path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from urbanlens.dashboard.external_api import views, views_messaging

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
    # Messaging. Every literal "messages/..." path MUST stay above the
    # "messages/<str:peer_slug>/" routes below: Django matches in order, and a
    # profile whose slug happened to be "settings", "groups" or
    # "conversations" would otherwise shadow the endpoint of that name (or,
    # worse, be shadowed by it). views_messaging.RESERVED_PEER_SLUGS refuses
    # those slugs as peers as well, so the two defenses have to both fail
    # before a request can be misrouted.
    path("messages/conversations/", views_messaging.ConversationsView.as_view(), name="messages.conversations"),
    path("messages/settings/", views_messaging.MessageSettingsView.as_view(), name="messages.settings"),
    path("messages/groups/", views_messaging.GroupsView.as_view(), name="messages.groups"),
    path("messages/groups/<uuid:group_uuid>/", views_messaging.GroupDetailView.as_view(), name="messages.groups.detail"),
    path("messages/groups/<uuid:group_uuid>/messages/", views_messaging.GroupMessagesView.as_view(), name="messages.groups.messages"),
    path("messages/groups/<uuid:group_uuid>/read/", views_messaging.GroupReadView.as_view(), name="messages.groups.read"),
    path("messages/groups/<uuid:group_uuid>/members/", views_messaging.GroupMembersView.as_view(), name="messages.groups.members"),
    path("messages/groups/<uuid:group_uuid>/share/pin/", views_messaging.GroupPinShareView.as_view(), name="messages.groups.share.pin"),
    # Peer-slug routes - the catch-all segment, hence last. The more specific
    # sub-paths still precede the bare thread route for the same reason.
    path("messages/<str:peer_slug>/read/", views_messaging.MessageThreadReadView.as_view(), name="messages.read"),
    path("messages/<str:peer_slug>/react/<int:message_id>/", views_messaging.MessageReactionView.as_view(), name="messages.react"),
    path("messages/<str:peer_slug>/messages/<int:message_id>/", views_messaging.MessageDetailView.as_view(), name="messages.detail"),
    path("messages/<str:peer_slug>/", views_messaging.MessageThreadView.as_view(), name="messages.thread"),
    # The machine-readable contract (and a browsable view of it) for exactly
    # this surface - internal endpoints are excluded by
    # schema.preprocess_external_api_only. Served without auth: the schema is
    # the published contract, not user data.
    path("schema/", SpectacularAPIView.as_view(authentication_classes=[], permission_classes=[]), name="schema"),
    path("docs/", SpectacularSwaggerView.as_view(authentication_classes=[], permission_classes=[], url_name="external_api:schema"), name="docs"),
]
