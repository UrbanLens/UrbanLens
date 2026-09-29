"""QuerySets and Managers for the device-scanning models."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from django.contrib.gis.measure import D
from django.db.models import Q

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from datetime import datetime

    from django.contrib.gis.geos import Point

    from urbanlens.dashboard.models.device_scan.model import DeviceScanEntry, DeviceScanUpload, DeviceSignalReading, ScannedDevice, WikiDeviceMarker  # noqa: F401 - mypy needs these; ruff does not
    from urbanlens.dashboard.models.wiki.model import Wiki


class ScannedDeviceQuerySet(abstract.FrontendDashboardQuerySet["ScannedDevice"]):
    """QuerySet for ScannedDevice."""


_ScannedDeviceManagerBase = abstract.FrontendDashboardManager.from_queryset(ScannedDeviceQuerySet)


class ScannedDeviceManager(_ScannedDeviceManagerBase["ScannedDevice"]):
    """Manager for ScannedDevice."""

    def get_or_create_for_mac(self, raw_mac_address: str) -> tuple[ScannedDevice, bool]:
        """Get or create the device identified by *raw_mac_address*, normalizing first.
        The single entry point for resolving a device by MAC - callers never normalize (or skip normalizing) on their own, which would otherwise risk two rows for the same physical device over a casing/separator difference between uploads.

        Args:
            raw_mac_address: MAC address as submitted by the client, in any
                case/separator style ``normalize_mac_address`` accepts.

        Returns:
            ``(device, created)``, exactly like ``get_or_create``.
        """
        from urbanlens.dashboard.services.device_scan.mac_address import normalize_mac_address

        return self.get_or_create(mac_address=normalize_mac_address(raw_mac_address))


class DeviceScanUploadQuerySet(abstract.FrontendDashboardQuerySet["DeviceScanUpload"]):
    """QuerySet for DeviceScanUpload."""

    def stalled(self, *, pending_before: datetime, claimed_before: datetime) -> Self:
        """Uploads nothing is working on: queued too long ago, or claimed by a worker that never finished.

        Args:
            pending_before: A pending upload created before this has lost its enqueue.
            claimed_before: A processing upload claimed before this has outlived any worker's time limit.

        Returns:
            This queryset filtered, oldest first.
        """
        from urbanlens.dashboard.models.device_scan.model import ScanUploadStatus

        return self.filter(Q(status=ScanUploadStatus.PENDING, created__lt=pending_before) | Q(status=ScanUploadStatus.PROCESSING, claimed_at__lt=claimed_before)).order_by("created")


_DeviceScanUploadManagerBase = abstract.FrontendDashboardManager.from_queryset(DeviceScanUploadQuerySet)


class DeviceScanUploadManager(_DeviceScanUploadManagerBase):
    """Manager for DeviceScanUpload."""


class DeviceScanEntryQuerySet(abstract.DashboardQuerySet["DeviceScanEntry"]):
    """QuerySet for DeviceScanEntry."""


_DeviceScanEntryManagerBase = abstract.DashboardManager.from_queryset(DeviceScanEntryQuerySet)


class DeviceScanEntryManager(_DeviceScanEntryManagerBase):
    """Manager for DeviceScanEntry."""


class DeviceSignalReadingQuerySet(abstract.DashboardQuerySet["DeviceSignalReading"]):
    """QuerySet for DeviceSignalReading."""


_DeviceSignalReadingManagerBase = abstract.DashboardManager.from_queryset(DeviceSignalReadingQuerySet)


class DeviceSignalReadingManager(_DeviceSignalReadingManagerBase):
    """Manager for DeviceSignalReading."""


class WikiDeviceMarkerQuerySet(abstract.FrontendDashboardQuerySet["WikiDeviceMarker"]):
    """QuerySet for WikiDeviceMarker."""

    def visible(self) -> Self:
        """Markers worth telling the mobile app about (excludes dismissed/presumed-removed)."""
        from urbanlens.dashboard.models.device_scan.model import MarkerStatus

        return self.filter(status__in=(MarkerStatus.ACTIVE, MarkerStatus.STALE))

    def for_wiki_and_device(self, wiki: Wiki, device: ScannedDevice) -> Self:
        """Non-dismissed markers for one (wiki, device) pair, for recompute reconciliation."""
        from urbanlens.dashboard.models.device_scan.model import MarkerStatus

        return self.filter(wiki=wiki, device=device).exclude(status=MarkerStatus.DISMISSED)

    def near(self, point: Point, radius_meters: float) -> Self:
        """Markers whose centroid falls within *radius_meters* of *point*."""
        return self.filter(centroid__dwithin=(point, D(m=radius_meters)))


_WikiDeviceMarkerManagerBase = abstract.FrontendDashboardManager.from_queryset(WikiDeviceMarkerQuerySet)


class WikiDeviceMarkerManager(_WikiDeviceMarkerManagerBase):
    """Manager for WikiDeviceMarker."""
