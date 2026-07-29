"""External-API routes for the mobile app's wireless device-scanning feature.

Two routes only: uploading scan data, and querying which devices are already
known nearby. No wiki-editing route exists here on purpose - marker
maintenance is entirely a background-task concern (see
``dashboard.tasks.process_device_scan_upload``).

Wiring: ``urls.py`` concatenates the ``urlpatterns`` below into the flat
``external_api:`` namespace and re-sorts the combined list with
:func:`~urbanlens.dashboard.external_api.urls.order_by_specificity`, so
declaration order inside this module only breaks ties between routes of
identical shape.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.urls import path

from urbanlens.dashboard.external_api import views_device_scans

if TYPE_CHECKING:
    from django.urls.resolvers import URLPattern

urlpatterns: list[URLPattern] = [
    path("device-scans/", views_device_scans.DeviceScanUploadView.as_view(), name="device_scans.upload"),
    path("device-scans/nearby/", views_device_scans.NearbyDeviceMarkersView.as_view(), name="device_scans.nearby"),
]
