"""Wireless device scanning: ingested scan data and the wiki markers it produces.

The mobile app's device-scanning feature uploads nearby-device readings (MAC
address, signal-strength samples along a route, a client-estimated location,
and a device-type guess) through the external API. A background task
(``dashboard.tasks.process_device_scan_upload``) turns camera/sensor/tracker
detections into :class:`WikiDeviceMarker` rows - fuzzy map markers that
tighten as more scans corroborate them - on every wiki whose boundary
contains the detection. See ``services.device_scan`` for the classification
and clustering logic; this module only holds the data.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib.gis.db.models import PointField
from django.db.models import CASCADE, SET_NULL, BooleanField, CharField, DateTimeField, FloatField, ForeignKey, Index, IntegerField, PositiveIntegerField, TextField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.device_scan.queryset import (
    DeviceScanEntryManager,
    DeviceScanUploadManager,
    DeviceSignalReadingManager,
    ScannedDeviceManager,
    WikiDeviceMarkerManager,
)


class DeviceType(abstract.TextChoices):
    """A wireless device's kind, as guessed by the client or a server heuristic."""

    CAMERA = "camera", "Camera"
    SENSOR = "sensor", "Sensor"
    TRACKER = "tracker", "Tracker"
    ACCESS_POINT = "access_point", "Access Point"
    PHONE = "phone", "Phone"
    WEARABLE = "wearable", "Wearable"
    IOT = "iot", "IoT Device"
    OTHER = "other", "Other"
    UNKNOWN = "unknown", "Unknown"


#: Device types that raise/maintain a WikiDeviceMarker. The single place this
#: is decided - adding a type later (e.g. drones) is a one-line change here,
#: with no model or migration impact.
SECURITY_RELEVANT_TYPES = frozenset({DeviceType.CAMERA, DeviceType.SENSOR, DeviceType.TRACKER})


class DeviceTypeSource(abstract.TextChoices):
    """How a ScannedDevice's current ``device_type`` was determined."""

    UNSET = "unset", "Unset"
    CLIENT = "client", "Reported by client"
    HEURISTIC = "heuristic", "Server heuristic"


class ScanUploadStatus(abstract.TextChoices):
    """Processing status of a DeviceScanUpload."""

    PENDING = "pending", "Pending"
    PROCESSED = "processed", "Processed"
    FAILED = "failed", "Failed"


class MarkerStatus(abstract.TextChoices):
    """Lifecycle status of a WikiDeviceMarker."""

    # Corroborated by observations within the clustering lookback window.
    ACTIVE = "active", "Active"
    # No observations within the lookback window recently, but never
    # explicitly reported absent - shown with reduced confidence.
    STALE = "stale", "Stale"
    # Enough consecutive "not detected" reports came in to believe the
    # device is gone (removed, turned off, or destroyed).
    PRESUMED_REMOVED = "presumed_removed", "Presumed Removed"
    # A wiki editor flagged this marker as a false positive. Terminal -
    # never recomputed or reconsidered again.
    DISMISSED = "dismissed", "Dismissed"


class ScannedDevice(abstract.FrontendDashboardModel):
    """One physical wireless device, identified by its MAC address.

    Global rather than per-profile: many users' scans corroborate the same
    device, and its classification/marker history is shared across all of
    them rather than siloed per uploader.
    """

    # Normalized upper-case colon-separated form (see
    # services.device_scan.mac_address.normalize_mac_address) - the only
    # form ever stored, so a lookup can never miss a match over a casing or
    # separator difference between two uploads of the same device.
    mac_address = CharField(max_length=17, unique=True, editable=False)
    display_name = CharField(max_length=255, blank=True, default="")
    device_type = CharField(max_length=20, choices=DeviceType.choices, default=DeviceType.UNKNOWN)
    device_type_source = CharField(max_length=20, choices=DeviceTypeSource.choices, default=DeviceTypeSource.UNSET)
    # No dedicated first_seen/last_seen fields - the inherited created/updated
    # (abstract.DashboardModel) already mean exactly that, since the only
    # writes to this model happen from scan ingestion touching the row.

    objects: ScannedDeviceManager = ScannedDeviceManager()

    def __str__(self) -> str:
        return f"ScannedDevice({self.mac_address}, {self.device_type})"

    class Meta(abstract.FrontendDashboardModel.Meta):
        db_table = "dashboard_scanned_devices"
        indexes = [Index(fields=["mac_address"], name="idxdb_scandev_mac")]


class DeviceScanUpload(abstract.FrontendDashboardModel):
    """One batch upload from the mobile app's device-scanning feature.

    ``profile`` is null whenever the uploading profile has
    ``Profile.track_device_scans`` turned off - the upload is still processed
    (classification/markers are shared community data), just without personal
    attribution. Authentication is always required to reach the upload
    endpoint regardless; this field only controls attribution.

    A ``FrontendDashboardModel`` (for its ``uuid``) rather than a plain
    ``DashboardModel``, so the upload endpoint's response can hand back an
    opaque identifier - matching every other external-API response in this
    app, none of which ever expose a raw integer pk.
    """

    profile = ForeignKey("dashboard.Profile", on_delete=SET_NULL, null=True, blank=True, related_name="device_scan_uploads")
    # Client-supplied idempotency/resume token. Stored verbatim for the
    # client's own troubleshooting; the server does not currently dedupe on it.
    client_session_uuid = CharField(max_length=64, blank=True, default="")
    status = CharField(max_length=20, choices=ScanUploadStatus.choices, default=ScanUploadStatus.PENDING)
    error = TextField(blank=True, default="")

    if TYPE_CHECKING:
        profile_id: int | None

    objects: DeviceScanUploadManager = DeviceScanUploadManager()

    def __str__(self) -> str:
        return f"DeviceScanUpload({self.pk}, {self.status})"

    class Meta(abstract.FrontendDashboardModel.Meta):
        db_table = "dashboard_device_scan_uploads"
        get_latest_by = "created"


class DeviceScanEntry(abstract.DashboardModel):
    """One device's data within one DeviceScanUpload.

    ``location`` is the device's client-estimated position when ``detected``
    is True, or the point the client searched near when reporting that an
    expected device wasn't found (``detected=False``).
    """

    upload = ForeignKey(DeviceScanUpload, on_delete=CASCADE, related_name="entries")
    device = ForeignKey(ScannedDevice, on_delete=CASCADE, related_name="scan_entries")
    device_type_guess = CharField(max_length=20, choices=DeviceType.choices, null=True, blank=True)
    detected = BooleanField(default=True)
    location = PointField(geography=True, srid=4326)
    # Set when the client is confirming/refuting a marker it learned about
    # from the nearby-devices endpoint - see external_api.views_device_scans.
    expected_marker = ForeignKey("dashboard.WikiDeviceMarker", on_delete=SET_NULL, null=True, blank=True, related_name="scan_entries")

    if TYPE_CHECKING:
        upload_id: int
        device_id: int
        expected_marker_id: int | None

    objects: DeviceScanEntryManager = DeviceScanEntryManager()

    def __str__(self) -> str:
        return f"DeviceScanEntry(upload={self.upload_id}, device={self.device_id})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_device_scan_entries"


class DeviceSignalReading(abstract.DashboardModel):
    """One raw (coordinate, signal strength) sample along a scan route.

    Kept for richness beyond what the v1 clustering algorithm consumes (a
    per-entry average signal strength) - future refinements (proper
    trilateration, a signal-strength heatmap) can mine this without a schema
    change, per the "store as much data as possible" requirement.
    """

    entry = ForeignKey(DeviceScanEntry, on_delete=CASCADE, related_name="readings")
    point = PointField(geography=True, srid=4326)
    signal_strength = IntegerField(null=True, blank=True)
    observed_at = DateTimeField()

    if TYPE_CHECKING:
        entry_id: int

    objects: DeviceSignalReadingManager = DeviceSignalReadingManager()

    def __str__(self) -> str:
        return f"DeviceSignalReading(entry={self.entry_id}, rssi={self.signal_strength})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_device_signal_readings"


class WikiDeviceMarker(abstract.FrontendDashboardModel):
    """A fuzzy (or manually pinpointed) device location on one wiki's map.

    Multiple ACTIVE rows can exist for the same (device, wiki) pair while a
    moved device's old and new locations are both still corroborated - see
    ``services.device_scan.clustering`` for how they're reconciled.

    ``centroid`` is the single source of truth for where this marker is
    displayed, whether it holds an automatically-computed fuzzy position or a
    manually placed one - a future wiki-editing UI sets ``manually_placed``
    and overwrites ``centroid``/``radius_meters`` directly (typically to a
    precise point and a near-zero radius) when a user pinpoints the device;
    the clustering recompute then leaves this row's position alone.
    """

    wiki = ForeignKey("dashboard.Wiki", on_delete=CASCADE, related_name="device_markers")
    device = ForeignKey(ScannedDevice, on_delete=CASCADE, related_name="markers")
    status = CharField(max_length=20, choices=MarkerStatus.choices, default=MarkerStatus.ACTIVE)

    centroid = PointField(geography=True, srid=4326)
    radius_meters = FloatField(default=0.0)
    manually_placed = BooleanField(default=False)

    confidence = FloatField(default=0.0)
    observation_count = PositiveIntegerField(default=0)
    # Consecutive "not detected" reports with no positive corroboration in
    # between - reset to 0 by any positive detection. Crossing
    # ABSENCE_STREAK_THRESHOLD (services.device_scan.clustering) flips status
    # to PRESUMED_REMOVED.
    absence_streak = PositiveIntegerField(default=0)
    avg_signal_strength = FloatField(null=True, blank=True)

    # Deliberately plain (not auto_now[_add]) and always set explicitly by
    # services.device_scan.clustering from the contributing entries' own
    # timestamps - unlike the inherited created/updated (this row's own
    # lifecycle), a marker's first recompute can already span scan data
    # going back to the lookback window's start.
    first_observed_at = DateTimeField()
    last_observed_at = DateTimeField()

    if TYPE_CHECKING:
        wiki_id: int
        device_id: int

    objects: WikiDeviceMarkerManager = WikiDeviceMarkerManager()

    def __str__(self) -> str:
        return f"WikiDeviceMarker(wiki={self.wiki_id}, device={self.device_id}, {self.status})"

    class Meta(abstract.FrontendDashboardModel.Meta):
        db_table = "dashboard_wiki_device_markers"
        indexes = [
            Index(fields=["wiki", "device"], name="idxdb_wikidevmark_wiki_dev"),
            Index(fields=["status"], name="idxdb_wikidevmark_status"),
        ]
