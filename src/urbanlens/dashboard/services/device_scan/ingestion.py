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
    with transaction.atomic():
        upload = DeviceScanUpload.objects.create(profile=attributed_profile, client_session_uuid=client_session_uuid)
        for device_data in devices:
            device, _created = ScannedDevice.objects.get_or_create_for_mac(device_data["mac_address"])
            device_name = (device_data.get("device_name") or "").strip()
            if device_name and device_name != device.display_name:
                device.display_name = device_name
                device.save(update_fields=["display_name", "updated"])

            expected_marker = None
            marker_uuid = device_data.get("expected_marker_uuid")
            if marker_uuid:
                expected_marker = WikiDeviceMarker.objects.filter(uuid=marker_uuid).first()

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
