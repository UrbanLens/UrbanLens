"""QuerySet/Manager for registered native push devices."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


class PushDeviceQuerySet(abstract.DashboardQuerySet):
    """QuerySet for :class:`~urbanlens.dashboard.models.push_device.model.PushDevice`."""

    def active(self) -> PushDeviceQuerySet:
        """Restrict to devices that can still receive pushes (not revoked)."""
        return self.filter(revoked_at__isnull=True)

    def for_profile(self, profile: Profile) -> PushDeviceQuerySet:
        """Restrict to one profile's registered devices.

        Args:
            profile: The owning profile.

        Returns:
            This queryset filtered to the profile's devices.
        """
        return self.filter(profile=profile)


class PushDeviceManager(abstract.DashboardManager.from_queryset(PushDeviceQuerySet)):
    """Manager for :class:`~urbanlens.dashboard.models.push_device.model.PushDevice`."""
