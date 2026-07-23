"""QuerySet/Manager for pin deletion tombstones."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from datetime import datetime, timedelta

    from urbanlens.dashboard.models.profile.model import Profile


class PinTombstoneQuerySet(abstract.DashboardQuerySet):
    """QuerySet for :class:`~urbanlens.dashboard.models.pin_tombstone.model.PinTombstone`."""

    def for_profile(self, profile: Profile) -> PinTombstoneQuerySet:
        """Restrict to tombstones for pins that belonged to ``profile``.

        Args:
            profile: The profile whose deleted pins to look up.

        Returns:
            This queryset filtered to the profile's tombstones.
        """
        return self.filter(profile=profile)

    def deleted_since(self, since: datetime) -> PinTombstoneQuerySet:
        """Restrict to pins deleted at or after ``since``.

        ``created`` is the deletion moment - a tombstone row is written in the
        same transaction as the pin's hard delete and never updated afterwards.

        Args:
            since: Inclusive lower bound on the deletion time.

        Returns:
            This queryset filtered to tombstones created at or after ``since``.
        """
        return self.filter(created__gte=since)


class PinTombstoneManager(abstract.DashboardManager.from_queryset(PinTombstoneQuerySet)):
    """Manager for :class:`~urbanlens.dashboard.models.pin_tombstone.model.PinTombstone`."""

    def record(self, *, profile_id: int, pin_uuid) -> None:
        """Record that the pin identified by ``pin_uuid`` was deleted.

        Idempotent - recording the same uuid twice keeps the original
        deletion timestamp, which is the correct sync semantic (the client
        cares that the pin is gone, not that a second delete was attempted).

        Args:
            profile_id: Primary key of the profile that owned the pin.
            pin_uuid: The deleted pin's public uuid.
        """
        self.get_or_create(pin_uuid=pin_uuid, defaults={"profile_id": profile_id})

    def prune_older_than(self, cutoff: timedelta) -> int:
        """Delete tombstones older than ``cutoff``, returning how many were removed.

        A sync client whose last sync predates the oldest retained tombstone
        can no longer trust deletions incrementally and must full-resync; any
        scheduled pruning must therefore keep tombstones at least as long as
        the longest plausible client offline window.

        Args:
            cutoff: Age beyond which tombstones are removed.

        Returns:
            Number of tombstone rows deleted.
        """
        from django.utils import timezone

        deleted, _ = self.filter(created__lt=timezone.now() - cutoff).delete()
        return deleted
