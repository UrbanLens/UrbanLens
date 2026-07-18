"""QuerySet/Manager shared by PinAutoRemoval and WikiAutoRemoval."""

from __future__ import annotations

from urbanlens.dashboard.models import abstract


def normalize_auto_removal_value(kind: str, value: str) -> str:
    """Normalize a value the same way for both recording and checking a tombstone.

    Alias/owner names are matched case-insensitively (mirroring the DB-level
    case-insensitive uniqueness on aliases/owners); label values are already
    a bare primary-key string and links are matched by their exact URL, so
    both are left as-is beyond trimming.
    """
    from urbanlens.dashboard.models.auto_removals.model import AutoRemovalKind

    value = value.strip()
    if kind in (AutoRemovalKind.ALIAS, AutoRemovalKind.OWNER):
        return value.casefold()
    return value


class AutoRemovalQuerySet(abstract.DashboardQuerySet):
    """QuerySet for the PinAutoRemoval/WikiAutoRemoval tombstone models."""

    def of_kind(self, kind: str) -> AutoRemovalQuerySet:
        """Restrict to tombstones of the given kind (see ``AutoRemovalKind``)."""
        return self.filter(kind=kind)


class AutoRemovalManager(abstract.DashboardManager.from_queryset(AutoRemovalQuerySet)):
    """Manager for the PinAutoRemoval/WikiAutoRemoval tombstone models.

    Callers pass the owning parent as a keyword matching the concrete model's
    FK field (``pin=...`` for ``PinAutoRemoval``, ``wiki=...`` for ``WikiAutoRemoval``).
    """

    def record(self, *, kind: str, value: str, **parent_kwargs) -> None:
        """Record that ``value`` was removed, so automatic creation code won't recreate it.

        Args:
            kind: One of ``AutoRemovalKind``'s values.
            value: The raw (un-normalized) value that was removed.
            **parent_kwargs: Exactly one of ``pin=<Pin>`` or ``wiki=<Wiki>``.
        """
        self.get_or_create(kind=kind, value=normalize_auto_removal_value(kind, value), **parent_kwargs)

    def was_removed(self, *, kind: str, value: str, **parent_kwargs) -> bool:
        """Return True when ``value`` was previously removed and must not be recreated.

        Args:
            kind: One of ``AutoRemovalKind``'s values.
            value: The raw (un-normalized) candidate value to check.
            **parent_kwargs: Exactly one of ``pin=<Pin>`` or ``wiki=<Wiki>``.
        """
        return self.filter(kind=kind, value=normalize_auto_removal_value(kind, value), **parent_kwargs).exists()
