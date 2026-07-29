"""Request/response types for the wireless device-scanning endpoints.

Hand-rolled and deliberately minimal, like every other serializer in this
package (see ``serializers``'s module docstring) - these never reuse an
internal serializer, so the external write surface can't silently widen when
an internal one grows a field.
"""

from __future__ import annotations

from rest_framework import serializers

from urbanlens.dashboard.models.device_scan.model import DeviceType
from urbanlens.dashboard.services.device_scan.mac_address import InvalidMacAddressError, normalize_mac_address

#: Hard ceiling on devices per upload - generous for a single walked route,
#: but bounded so one malformed/malicious payload can't force an unbounded
#: number of DB writes inline in the request.
MAX_DEVICES_PER_UPLOAD = 200

#: Same reasoning, per device: a walked route's signal-strength trail is
#: naturally bounded by how long a scan session runs.
MAX_READINGS_PER_DEVICE = 500


class DeviceSignalReadingInputSerializer(serializers.Serializer):
    """One raw (coordinate, signal strength) sample along a scan route."""

    latitude = serializers.FloatField(min_value=-90, max_value=90)
    longitude = serializers.FloatField(min_value=-180, max_value=180)
    signal_strength = serializers.IntegerField(required=False, allow_null=True, default=None, min_value=-150, max_value=0)
    observed_at = serializers.DateTimeField()


class DeviceScanEntryInputSerializer(serializers.Serializer):
    """One device's data within a scan upload.

    ``detected=False`` reports that an expected device (see
    ``expected_marker_uuid``, sourced from a prior ``device-scans/nearby/``
    response) was searched for near (``estimated_latitude``,
    ``estimated_longitude``) but not found - it carries no ``readings``.
    """

    mac_address = serializers.CharField(max_length=32)
    device_name = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    device_type_guess = serializers.ChoiceField(choices=DeviceType.choices, required=False, allow_null=True, default=None)
    detected = serializers.BooleanField(default=True)
    estimated_latitude = serializers.FloatField(min_value=-90, max_value=90)
    estimated_longitude = serializers.FloatField(min_value=-180, max_value=180)
    expected_marker_uuid = serializers.UUIDField(required=False, allow_null=True, default=None)
    readings = DeviceSignalReadingInputSerializer(many=True, required=False, default=list)

    def validate_mac_address(self, value: str) -> str:
        """Normalize and validate the MAC address, so downstream code never re-normalizes."""
        try:
            return normalize_mac_address(value)
        except InvalidMacAddressError as exc:
            raise serializers.ValidationError(str(exc)) from exc

    def validate_readings(self, value: list[dict]) -> list[dict]:
        """Reject an unreasonably long route trail rather than truncating it silently."""
        if len(value) > MAX_READINGS_PER_DEVICE:
            raise serializers.ValidationError(f"At most {MAX_READINGS_PER_DEVICE} readings per device.")
        return value


class DeviceScanUploadSerializer(serializers.Serializer):
    """Validates one device-scan upload batch."""

    client_session_uuid = serializers.CharField(max_length=64, required=False, allow_blank=True, default="")
    devices = DeviceScanEntryInputSerializer(many=True)

    def validate_devices(self, value: list[dict]) -> list[dict]:
        """At least one device, and no more than :data:`MAX_DEVICES_PER_UPLOAD`."""
        if not value:
            raise serializers.ValidationError("At least one device is required.")
        if len(value) > MAX_DEVICES_PER_UPLOAD:
            raise serializers.ValidationError(f"At most {MAX_DEVICES_PER_UPLOAD} devices per upload.")
        return value


class DeviceScanUploadResponseSerializer(serializers.Serializer):
    """Acknowledgment of an accepted upload - processing happens in the background."""

    upload_uuid = serializers.UUIDField(read_only=True)


class NearbyDeviceMarkerDeviceSerializer(serializers.Serializer):
    """The device identity portion of a nearby-marker entry."""

    mac_address = serializers.CharField(read_only=True)
    device_type = serializers.CharField(read_only=True)
    display_name = serializers.CharField(read_only=True)


class NearbyDeviceMarkerSerializer(serializers.Serializer):
    """One wiki device marker, as reported to the mobile app.

    ``marker_uuid`` is what a later upload's ``expected_marker_uuid`` refers
    back to when the client confirms or refutes this exact marker.
    """

    marker_uuid = serializers.UUIDField(source="uuid", read_only=True)
    device = NearbyDeviceMarkerDeviceSerializer(read_only=True)
    latitude = serializers.SerializerMethodField()
    longitude = serializers.SerializerMethodField()
    radius_meters = serializers.FloatField(read_only=True)
    confidence = serializers.FloatField(read_only=True)
    avg_signal_strength = serializers.FloatField(read_only=True, allow_null=True)
    last_observed_at = serializers.DateTimeField(read_only=True)
    status = serializers.CharField(read_only=True)

    def get_latitude(self, marker) -> float:
        return marker.centroid.y

    def get_longitude(self, marker) -> float:
        return marker.centroid.x


class NearbyDeviceMarkersQuerySerializer(serializers.Serializer):
    """Query params for ``GET device-scans/nearby/``."""

    latitude = serializers.FloatField(min_value=-90, max_value=90)
    longitude = serializers.FloatField(min_value=-180, max_value=180)
    radius_meters = serializers.FloatField(required=False, default=500.0, min_value=1, max_value=50_000)


class NearbyDeviceMarkersResponseSerializer(serializers.Serializer):
    """Wrapper response for the nearby-devices query."""

    markers = NearbyDeviceMarkerSerializer(many=True, read_only=True)
