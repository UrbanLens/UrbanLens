"""External-API views for the mobile app's wireless device-scanning feature.

Two endpoints only, per design - no wiki-editing surface: an upload endpoint
that ingests scan data (classification and marker maintenance happen
entirely in the background, see ``dashboard.tasks.process_device_scan_upload``),
and a read endpoint telling the app which devices are already known nearby.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from django.contrib.gis.geos import Point
from django.db import transaction
from drf_spectacular.utils import extend_schema
from rest_framework.response import Response

from urbanlens.dashboard.external_api.serializers import ErrorSerializer
from urbanlens.dashboard.external_api.serializers_device_scans import (
    DeviceScanUploadResponseSerializer,
    DeviceScanUploadSerializer,
    NearbyDeviceMarkersQuerySerializer,
    NearbyDeviceMarkersResponseSerializer,
)
from urbanlens.dashboard.external_api.views import ExternalApiView
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.device_scan.model import WikiDeviceMarker
from urbanlens.dashboard.services.core.celery import safely_enqueue_task
from urbanlens.dashboard.services.device_scan.ingestion import ingest_scan_upload
from urbanlens.dashboard.services.wiki.wiki_access import visible_wiki_location_ids

if TYPE_CHECKING:
    from rest_framework.request import Request


class DeviceScanUploadView(ExternalApiView):
    """POST: upload one batch of wireless device-scan data.

    Authentication is always required, but whether this upload is attributed
    to the caller's account depends entirely on their own
    ``Profile.track_device_scans`` preference - an anonymous upload is still
    fully processed (classification and community wiki markers don't need an
    owner), it just isn't linked to anyone.

    Nothing in the response describes what the scan found: classification,
    wiki matching, and marker clustering all happen in the background (see
    ``dashboard.tasks.process_device_scan_upload``) - a client that wants to
    know what's nearby calls ``device-scans/nearby/`` instead.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.DEVICE_SCANS_WRITE}),
    }

    @extend_schema(request=DeviceScanUploadSerializer, responses={202: DeviceScanUploadResponseSerializer, 400: ErrorSerializer})
    def post(self, request: Request) -> Response:
        """Validate and persist the upload, then queue it for background processing."""
        serializer = DeviceScanUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        profile = request.user.profile

        attributed_profile = profile if profile.track_device_scans else None
        upload = ingest_scan_upload(
            attributed_profile,
            client_session_uuid=data.get("client_session_uuid", ""),
            devices=data["devices"],
        )

        def _enqueue() -> None:
            from urbanlens.dashboard.tasks import process_device_scan_upload

            safely_enqueue_task(process_device_scan_upload, upload.pk)

        transaction.on_commit(_enqueue)

        return Response({"upload_uuid": str(upload.uuid)}, status=202)


class NearbyDeviceMarkersView(ExternalApiView):
    """GET: wireless device markers already known near a point.

    Lets the app decide when to turn scanning on (entering a region with
    known markers) and enrich what it shows the user when it detects a
    matching MAC address. Scoped to wikis the caller can actually see - a
    device marker is wiki-scoped content, and this API's whole security model
    is that an undiscovered wiki is invisible, enforced identically here via
    ``services.wiki.wiki_access.visible_wiki_location_ids``.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.DEVICE_SCANS_READ}),
    }

    @extend_schema(parameters=[NearbyDeviceMarkersQuerySerializer], responses={200: NearbyDeviceMarkersResponseSerializer, 400: ErrorSerializer})
    def get(self, request: Request) -> Response:
        """Return active/stale device markers within ``radius_meters`` of (latitude, longitude)."""
        serializer = NearbyDeviceMarkersQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        params = serializer.validated_data

        point = Point(params["longitude"], params["latitude"], srid=4326)
        visible_location_ids = visible_wiki_location_ids(request.user.profile)

        markers = WikiDeviceMarker.objects.visible().near(point, params["radius_meters"]).filter(wiki__officially_created=True, wiki__location_id__in=visible_location_ids).select_related("device", "wiki__location")

        return Response(NearbyDeviceMarkersResponseSerializer({"markers": list(markers)}).data)
