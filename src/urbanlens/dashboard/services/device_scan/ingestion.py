"""Persist a validated device-scan upload; classification/clustering happens later, in the background.

Kept deliberately lightweight - this runs inline in the upload request/response
cycle, so it only writes what the client submitted. Device-type resolution,
wiki matching, and marker clustering are comparatively heavy (potentially many
DB queries per device) and run out-of-band in
``dashboard.tasks.process_device_scan_upload``, matching how every other
upload endpoint in this API defers its processing (e.g. ``PhotosView.post``
+ ``process_image_upload``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib.gis.geos import Point
from django.db import transaction

from urbanlens.dashboard.models.device_scan.model import DeviceScanEntry, DeviceScanUpload, DeviceSignalReading, ScannedDevice, WikiDeviceMarker

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


def ingest_scan_upload(attributed_profile: Profile | None, *, client_session_uuid: str, devices: list[dict[str, Any]]) -> DeviceScanUpload:
    """Persist one validated device-scan upload and its per-device entries/readings.

    Args:
        attributed_profile: The profile to attribute this upload to, or None
            for an anonymous upload (the caller has already applied
            ``Profile.track_device_scans`` - this function does not
            re-check it, so it can be tested independently of that policy).
        client_session_uuid: Client-supplied idempotency/resume token, or "".
        devices: Validated device entries from ``DeviceScanUploadSerializer``
            - each a dict with ``mac_address``, optional ``device_name``/
            ``device_type_guess``, ``detected``, ``estimated_latitude``/
            ``estimated_longitude``, optional ``expected_marker_uuid``, and
            an optional ``readings`` list.

    Returns:
        The created DeviceScanUpload, with its entries/readings already saved.
    """
    # Resolved in one query instead of one per device: an upload carries up to
    # MAX_DEVICES_PER_UPLOAD entries, so the per-device lookup this replaces was
    # up to 200 round-trips inside a single synchronous request.
    marker_uuids = {device_data["expected_marker_uuid"] for device_data in devices if device_data.get("expected_marker_uuid")}
    # Keyed by str: the ORM lookup this replaced coerced either a string or a
    # UUID, and this service is called directly as well as through the serializer
    # (which hands over a real UUID) - a dict keyed on UUID objects alone would
    # silently resolve nothing for a string caller.
    markers_by_uuid = {str(marker.uuid): marker for marker in WikiDeviceMarker.objects.filter(uuid__in=marker_uuids)} if marker_uuids else {}

    with transaction.atomic():
        upload = DeviceScanUpload.objects.create(profile=attributed_profile, client_session_uuid=client_session_uuid)
        for device_data in devices:
            device, _created = ScannedDevice.objects.get_or_create_for_mac(device_data["mac_address"])
            device_name = (device_data.get("device_name") or "").strip()
            if device_name and device_name != device.display_name:
                device.display_name = device_name
                device.save(update_fields=["display_name", "updated"])

            marker_uuid = device_data.get("expected_marker_uuid")
            # .get() not [] - an unknown uuid stays None, exactly as the
            # per-device .first() did, rather than failing the whole upload.
            expected_marker = markers_by_uuid.get(str(marker_uuid)) if marker_uuid else None

            entry = DeviceScanEntry.objects.create(
                upload=upload,
                device=device,
                device_type_guess=device_data.get("device_type_guess") or None,
                detected=device_data.get("detected", True),
                location=Point(float(device_data["estimated_longitude"]), float(device_data["estimated_latitude"]), srid=4326),
                expected_marker=expected_marker,
            )

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
    return upload
