from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)


class QuerySet(abstract.DashboardQuerySet):
    """Custom queryset for Review."""

    def for_pair(self, profile: Profile, pin: Pin) -> QuerySet:
        """The review row (at most one) for a pair."""
        return self.filter(profile=profile, pin=pin)


class Manager(abstract.DashboardManager.from_queryset(QuerySet)):
    """Custom manager for Review."""
