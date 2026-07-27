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
    # Safety check-ins. The three literal "safety/..." roots below can't collide
    # with each other, but "safety/checkins/<slug>/" must stay after
    # "safety/checkins/" or the generic segment swallows it - same ordering rule
    # as the pins routes above. The sub-resource routes each carry an extra path
    # segment, so they're matched before the bare detail route can claim them.
    path("safety/checkins/", views.SafetyCheckinsView.as_view(), name="safety.checkins"),
    path("safety/contacts/", views.SafetyContactDefaultsView.as_view(), name="safety.contacts"),
    path("safety/settings/", views.SafetyPreferencesView.as_view(), name="safety.settings"),
    path("safety/checkins/<str:checkin_slug>/check-in/", views.SafetyCheckinMarkSafeView.as_view(), name="safety.checkins.check_in"),
    path("safety/checkins/<str:checkin_slug>/cancel/", views.SafetyCheckinCancelApiView.as_view(), name="safety.checkins.cancel"),
    path("safety/checkins/<str:checkin_slug>/partners/", views.SafetyCheckinPartnersApiView.as_view(), name="safety.checkins.partners"),
    path("safety/checkins/<str:checkin_slug>/partners/<int:partner_id>/", views.SafetyCheckinPartnerDetailApiView.as_view(), name="safety.checkins.partners.detail"),
    path("safety/checkins/<str:checkin_slug>/photos/", views.SafetyCheckinPhotosView.as_view(), name="safety.checkins.photos"),
    path("safety/checkins/<str:checkin_slug>/photos/<int:image_id>/", views.SafetyCheckinPhotoDetailView.as_view(), name="safety.checkins.photos.detail"),
    path("safety/checkins/<str:checkin_slug>/maps/", views.SafetyCheckinMapsView.as_view(), name="safety.checkins.maps"),
    path("safety/checkins/<str:checkin_slug>/maps/<uuid:map_uuid>/", views.SafetyCheckinMapDetailView.as_view(), name="safety.checkins.maps.detail"),
    # Must stay last of the "safety/checkins/..." group, for the same reason.
    path("safety/checkins/<str:checkin_slug>/", views.SafetyCheckinDetailApiView.as_view(), name="safety.checkins.detail"),
    # The machine-readable contract (and a browsable view of it) for exactly
    # this surface - internal endpoints are excluded by
    # schema.preprocess_external_api_only. Served without auth: the schema is
    # the published contract, not user data.
    path("schema/", SpectacularAPIView.as_view(authentication_classes=[], permission_classes=[]), name="schema"),
    path("docs/", SpectacularSwaggerView.as_view(authentication_classes=[], permission_classes=[], url_name="external_api:schema"), name="docs"),
]
