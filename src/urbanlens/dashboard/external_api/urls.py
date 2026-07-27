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
    path("lists/", views.PinListsView.as_view(), name="lists"),
    # The three literal sub-paths below must all stay ahead of the generic
    # "lists/<slug:list_slug>/" route: Django matches in order, and a list
    # slug would otherwise swallow "items"/"reorder"/"resync" as if they were
    # list identifiers. Within the items group, the literal "reorder/" segment
    # likewise precedes nothing generic, but is kept adjacent for clarity.
    path("lists/<slug:list_slug>/items/reorder/", views.PinListItemsReorderView.as_view(), name="lists.items.reorder"),
    path("lists/<slug:list_slug>/items/", views.PinListItemsView.as_view(), name="lists.items"),
    path("lists/<slug:list_slug>/resync/", views.PinListResyncView.as_view(), name="lists.resync"),
    path("lists/<slug:list_slug>/", views.PinListDetailView.as_view(), name="lists.detail"),
    path("saved-filters/", views.SavedFiltersView.as_view(), name="saved_filters"),
    path("saved-filters/<uuid:filter_uuid>/", views.SavedFilterDetailView.as_view(), name="saved_filters.detail"),
    path("labels/", views.LabelsView.as_view(), name="labels"),
    # Same ordering rule as the lists group above - the two literal sub-paths
    # precede the generic label-detail route. A <uuid> converter would not in
    # fact match "customization", but the ordering is kept explicit so the
    # convention survives a future switch to a looser converter.
    path("labels/<uuid:label_uuid>/customization/", views.LabelCustomizationView.as_view(), name="labels.customization"),
    path("labels/<uuid:label_uuid>/merge/", views.LabelMergeView.as_view(), name="labels.merge"),
    path("labels/<uuid:label_uuid>/", views.LabelDetailView.as_view(), name="labels.detail"),
    path("push-devices/", views.PushDevicesView.as_view(), name="push_devices"),
    path("push-devices/<uuid:device_uuid>/", views.PushDeviceDetailView.as_view(), name="push_devices.detail"),
    # The machine-readable contract (and a browsable view of it) for exactly
    # this surface - internal endpoints are excluded by
    # schema.preprocess_external_api_only. Served without auth: the schema is
    # the published contract, not user data.
    path("schema/", SpectacularAPIView.as_view(authentication_classes=[], permission_classes=[]), name="schema"),
    path("docs/", SpectacularSwaggerView.as_view(authentication_classes=[], permission_classes=[], url_name="external_api:schema"), name="docs"),
]
