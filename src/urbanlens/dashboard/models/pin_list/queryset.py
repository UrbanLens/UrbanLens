"""QuerySet and Manager for PinList."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import Count, Q

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin_list.model import PinList, PinListItem, SmartListSyncRequest  # noqa: F401 - mypy needs these; ruff does not
    from urbanlens.dashboard.models.profile.model import Profile


class PinListQuerySet(abstract.PublicDashboardQuerySet["PinList"]):
    """Custom queryset for PinList models."""

    def with_pin_counts(self) -> PinListQuerySet:
        """Annotate each list's pin count so ``pin_count`` costs no rows.

        The alternative a caller reaches for is ``prefetch_related("items")``,
        which fetches every membership row (and with ``items__pin``, every pin)
        to produce one integer per list.

        Returns:
            The queryset with ``_pin_count`` annotated, which
            :attr:`~urbanlens.dashboard.models.pin_list.model.PinList.pin_count`
            prefers over counting rows in Python.
        """
        return self.annotate(_pin_count=Count("items", distinct=True))

    def for_profile(self, profile: Profile | int) -> PinListQuerySet:
        """Every list owned by ``profile`` (accepts a Profile instance or a raw pk).

        Args:
            profile: The owning profile.

        Returns:
            Matching lists, unordered (callers apply their own ordering/prefetch).
        """
        return self.filter(profile=profile)

    def active_smart_lists(self, profile: Profile | int) -> PinListQuerySet:
        """The profile's smart lists that actually have matching rules configured.
        A smart list with neither ``smart_filter`` nor ``smart_boundary`` set has nothing to auto-match against yet, so callers syncing smart-list membership only need to consider lists with at least one of the two.

        Args:
            profile: The owning profile.

        Returns:
            Matching smart lists.
        """
        return self.for_profile(profile).filter(is_smart=True).filter(Q(smart_filter__isnull=False) | Q(smart_boundary__isnull=False))


_PinListManagerBase = abstract.PublicDashboardManager.from_queryset(PinListQuerySet)


class PinListManager(_PinListManagerBase):
    """Custom query manager for PinList models."""


class PinListItemQuerySet(abstract.DashboardQuerySet["PinListItem"]):
    """Custom queryset for PinListItem models."""

    def for_list(self, pin_list) -> PinListItemQuerySet:
        """Every item on one list.

        Args:
            pin_list: The list (accepts a PinList instance or a raw pk).

        Returns:
            Matching items, unordered (callers apply further filtering/ordering).
        """
        return self.filter(pin_list=pin_list)

    def membership(self, pin_list, pin):
        """This pin's membership row on this list, if any.

        Args:
            pin_list: The list to check.
            pin: The pin to check.

        Returns:
            The matching PinListItem, or None.
        """
        return self.for_list(pin_list).filter(pin=pin).first()


_PinListItemManagerBase = abstract.DashboardManager.from_queryset(PinListItemQuerySet)


class PinListItemManager(_PinListItemManagerBase):
    """Custom query manager for PinListItem models."""


class SmartListSyncRequestQuerySet(abstract.DashboardQuerySet["SmartListSyncRequest"]):
    """Custom queryset for SmartListSyncRequest models."""

    def for_profile(self, profile: Profile | int) -> SmartListSyncRequestQuerySet:
        """Every outstanding request for one account's pins.

        Args:
            profile: The owning profile (a Profile instance or a raw pk).

        Returns:
            Matching requests, unordered.
        """
        return self.filter(profile=profile)


_SmartListSyncRequestManagerBase = abstract.DashboardManager.from_queryset(SmartListSyncRequestQuerySet)


class SmartListSyncRequestManager(_SmartListSyncRequestManagerBase):
    """Custom query manager for SmartListSyncRequest models."""
