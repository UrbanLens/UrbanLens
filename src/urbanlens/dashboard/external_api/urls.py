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
    # A pin's sub-resources. These sit after the detail route above only for
    # readability - each has a literal trailing segment, so none of them can be
    # shadowed by it.
    path("pins/<str:pin_slug>/notes/", views.PinNotesView.as_view(), name="pins.notes"),
    path("pins/<str:pin_slug>/notes/<int:note_id>/", views.PinNoteDetailView.as_view(), name="pins.notes.detail"),
    path("pins/<str:pin_slug>/aliases/", views.PinAliasesView.as_view(), name="pins.aliases"),
    path("pins/<str:pin_slug>/aliases/<int:alias_id>/", views.PinAliasDetailView.as_view(), name="pins.aliases.detail"),
    path("pins/<str:pin_slug>/aliases/<int:alias_id>/use/", views.PinAliasUseView.as_view(), name="pins.aliases.use"),
    path("pins/<str:pin_slug>/links/", views.PinLinksView.as_view(), name="pins.links"),
    path("pins/<str:pin_slug>/links/<int:link_id>/", views.PinLinkDetailView.as_view(), name="pins.links.detail"),
    path("pins/<str:pin_slug>/visits/", views.PinVisitsView.as_view(), name="pins.visits"),
    path("pins/<str:pin_slug>/visits/<int:visit_id>/", views.PinVisitDetailView.as_view(), name="pins.visits.detail"),
    path("locations/search/", views.LocationSearchView.as_view(), name="locations.search"),
    path("locations/resolve/", views.PlaceResolveView.as_view(), name="locations.resolve"),
    path("pin-suggestions/", views.PinSuggestionsView.as_view(), name="pin_suggestions"),
    path("push-devices/", views.PushDevicesView.as_view(), name="push_devices"),
    path("push-devices/<uuid:device_uuid>/", views.PushDeviceDetailView.as_view(), name="push_devices.detail"),
    # The machine-readable contract (and a browsable view of it) for exactly
    # this surface - internal endpoints are excluded by
    # schema.preprocess_external_api_only. Served without auth: the schema is
    # the published contract, not user data.
    path("schema/", SpectacularAPIView.as_view(authentication_classes=[], permission_classes=[]), name="schema"),
    path("docs/", SpectacularSwaggerView.as_view(authentication_classes=[], permission_classes=[], url_name="external_api:schema"), name="docs"),
]
