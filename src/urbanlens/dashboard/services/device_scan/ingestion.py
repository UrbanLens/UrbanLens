"""Persist a validated device-scan upload; classification/clustering happens later, in the background.
Kept deliberately lightweight - this runs inline in the upload request/response cycle, so it only writes what the client submitted."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib.gis.geos import MultiPoint, Point
from django.db import IntegrityError, transaction

from urbanlens.dashboard.models.device_scan.model import DeviceScanEntry, DeviceScanUpload, DeviceSignalReading, ScannedDevice, WikiDeviceMarker

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


def ingest_scan_upload(uploader: Profile, *, attribute: bool, client_session_uuid: str, devices: list[dict[str, Any]]) -> tuple[DeviceScanUpload, bool]:
    """Persist one validated device-scan upload and its per-device entries/readings.

    A non-empty *client_session_uuid* is an idempotency key: a retry carrying one already stored
    returns that upload and writes nothing.

    Args:
        uploader: The profile sending the upload. It decides which wikis the upload may add evidence to, and is
            stored only when *attribute* is set.
        attribute: Whether to keep the upload attributed to *uploader*; the caller applies
            ``Profile.track_device_scans``, so this can be tested independently of that policy.
        client_session_uuid: Client-supplied idempotency key, one per upload batch, or "".
        devices: Validated device entries from ``DeviceScanUploadSerializer`` - each a dict with ``mac_address``, optional ``device_name``/ ``device_type_guess``, ``detected``, ``estimated_latitude``/ ``estimated_longitude``, optional ``expected_marker_uuid``,...

    Returns:
        The ``(upload, created)`` pair; ``created`` is False for a replay."""
    if client_session_uuid and (existing := DeviceScanUpload.objects.filter(client_session_uuid=client_session_uuid).first()) is not None:
        return existing, False
    try:
        return _ingest(uploader, attribute=attribute, client_session_uuid=client_session_uuid, devices=devices), True
    except IntegrityError:
        if not client_session_uuid:
            raise
        # A concurrent retry of the same batch committed first.
        return DeviceScanUpload.objects.get(client_session_uuid=client_session_uuid), False


def _routable_wiki_ids(uploader: Profile, points: list[Point]) -> list[int]:
    """Wikis any of *points* falls in that *uploader* can see."""
    from urbanlens.dashboard.models.place.model import Place
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.services.wiki.wiki_access import visible_wiki_locations

    if not points:
        return []
    places = Place.objects.current().filter(geometry__isnull=False, geometry__intersects=MultiPoint(points, srid=4326)).values("pk")
    return list(Wiki.objects.filter(place_id__in=places, location_id__in=visible_wiki_locations(uploader)).values_list("pk", flat=True))


def _ingest(uploader: Profile, *, attribute: bool, client_session_uuid: str, devices: list[dict[str, Any]]) -> DeviceScanUpload:
    # Resolved in one query instead of one per device: an upload carries up to
    # MAX_DEVICES_PER_UPLOAD entries, so the per-device lookup this replaces was
    # up to 200 round-trips inside a single synchronous request.
    marker_uuids = {device_data["expected_marker_uuid"] for device_data in devices if device_data.get("expected_marker_uuid")}
    # Keyed by str: the ORM lookup this replaced coerced either a string or a UUID, and this service
    # is called directly as well as through the serializer (which hands over a real UUID) - a dict
    # keyed on UUID objects alone would silently resolve nothing for a string caller.
    markers_by_uuid = {str(marker.uuid): marker for marker in WikiDeviceMarker.objects.filter(uuid__in=marker_uuids)} if marker_uuids else {}

    with transaction.atomic():
        upload = DeviceScanUpload.objects.create(profile=uploader if attribute else None, client_session_uuid=client_session_uuid, routing_recorded=True)
        points: list[Point] = []
        for device_data in devices:
            device, _created = ScannedDevice.objects.get_or_create_for_mac(device_data["mac_address"])

            marker_uuid = device_data.get("expected_marker_uuid")
            # .get() not [] - an unknown uuid stays None, exactly as the
            # per-device .first() did, rather than failing the whole upload.
            expected_marker = markers_by_uuid.get(str(marker_uuid)) if marker_uuid else None

            entry = DeviceScanEntry.objects.create(
                upload=upload,
                device=device,
                device_type_guess=device_data.get("device_type_guess") or None,
                device_name=(device_data.get("device_name") or "").strip(),
                detected=device_data.get("detected", True),
                location=Point(float(device_data["estimated_longitude"]), float(device_data["estimated_latitude"]), srid=4326),
                expected_marker=expected_marker,
            )
            points.append(entry.location)

            readings = device_data.get("readings") or []
            if readings:
                DeviceSignalReading.objects.bulk_create(
                    DeviceSignalReading(
                        entry=entry,
                        point=Point(float(reading["longitude"]), float(reading["latitude"]), srid=4326),
                        signal_strength=reading.get("signal_strength"),
                        observed_at=reading["observed_at"],
                    )
                    for reading in readings
                )
        upload.routable_wikis.set(_routable_wiki_ids(uploader, points))
    return upload
