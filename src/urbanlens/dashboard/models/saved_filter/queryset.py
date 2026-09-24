from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self
import uuid

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)


class SavedFilterQuerySet(abstract.DashboardQuerySet):
    """Custom queryset for SavedFilter models."""

    def name_taken_for(self, profile: Profile, name: str, *, exclude_pk: int | None = None) -> bool:
        """Whether ``profile`` already has a saved filter with ``name``.

        Args:
            profile: The owning profile.
            name: The candidate name (matched case-sensitively, as stored).
            exclude_pk: A saved filter pk to exclude from the check - pass the
                filter's own pk when validating a rename so it doesn't
                collide with itself.

        Returns:
            True if another saved filter of ``profile``'s already has that name.
        """
        qs = self.filter(profile=profile, name=name)
        if exclude_pk is not None:
            qs = qs.exclude(pk=exclude_pk)
        return qs.exists()

    def for_client_ids(self, profile: Profile, raw_ids: str) -> Self:
        """``profile``'s filters named by a comma-separated uuid list from the client.

        Another profile's uuid, an unknown uuid and a malformed value are all dropped the same way, so the
        result never says which of them a value was.

        Args:
            profile: The requesting profile; nothing outside it is returned.
            raw_ids: Comma-separated ``SavedFilter`` uuids, or "".

        Returns:
            The matching filters, possibly none.
        """
        ids = set()
        for value in raw_ids.split(","):
            try:
                ids.add(uuid.UUID(value.strip()))
            except ValueError:
                continue
        if not ids:
            return self.none()
        return self.filter(profile=profile, uuid__in=ids).select_related("profile")


class SavedFilterManager(abstract.DashboardManager.from_queryset(SavedFilterQuerySet)):
    """Custom query manager for SavedFilter models."""
