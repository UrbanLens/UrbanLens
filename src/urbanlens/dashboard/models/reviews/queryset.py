# Generic imports
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

# Django Imports
# App Imports
from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)


class QuerySet(abstract.DashboardQuerySet):
    """
    A custom queryset. All models below will use this for interacting with results from the db.
    """

    def for_pair(self, profile: Profile, pin: Pin) -> QuerySet:
        """The review row (at most one - unique on profile+pin) for a pair.

        Args:
            profile: The reviewing profile.
            pin: The reviewed pin.

        Returns:
            A queryset matching at most one row.
        """
        return self.filter(profile=profile, pin=pin)


class Manager(abstract.DashboardManager.from_queryset(QuerySet)):
    """
    A custom query manager. This creates QuerySets and is used in all models interacting with the app db.
    """
