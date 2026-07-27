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
    path("photos/", views.PhotosView.as_view(), name="photos"),
    # The <uuid:...> converter only matches a well-formed uuid, so these can't
    # shadow the literal "photos/" route above - but they stay after it to
    # keep this file's literal-before-generic ordering readable.
    path("photos/<uuid:image_uuid>/", views.PhotoDetailView.as_view(), name="photos.detail"),
    path("photos/<uuid:image_uuid>/labels/", views.PhotoLabelsView.as_view(), name="photos.labels"),
    path("photos/<uuid:image_uuid>/vote/", views.PhotoVoteView.as_view(), name="photos.vote"),
    path("photos/<uuid:image_uuid>/file/", views.PhotoFileView.as_view(), name="photos.file"),
    path("suggestions/visits/", views.VisitSuggestionsView.as_view(), name="suggestions.visits"),
    path("suggestions/visits/<int:suggestion_id>/<str:action>/", views.VisitSuggestionActionView.as_view(), name="suggestions.visits.action"),
    path("memories/journal/", views.MemoriesJournalView.as_view(), name="memories.journal"),
    path("push-devices/", views.PushDevicesView.as_view(), name="push_devices"),
    path("push-devices/<uuid:device_uuid>/", views.PushDeviceDetailView.as_view(), name="push_devices.detail"),
    # The machine-readable contract (and a browsable view of it) for exactly
    # this surface - internal endpoints are excluded by
    # schema.preprocess_external_api_only. Served without auth: the schema is
    # the published contract, not user data.
    path("schema/", SpectacularAPIView.as_view(authentication_classes=[], permission_classes=[]), name="schema"),
    path("docs/", SpectacularSwaggerView.as_view(authentication_classes=[], permission_classes=[], url_name="external_api:schema"), name="docs"),
]
