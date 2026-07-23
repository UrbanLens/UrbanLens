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
    path("pins/", views.PinsView.as_view(), name="pins"),
    path("pins/deleted/", views.PinTombstonesView.as_view(), name="pins.deleted"),
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
