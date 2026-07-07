"""Queryset and manager for PinMarkup."""

from __future__ import annotations

import logging
from typing import Self

from urbanlens.dashboard.models import abstract

logger = logging.getLogger(__name__)


class PinMarkupQuerySet(abstract.FrontendDashboardQuerySet):
    """QuerySet for PinMarkup map annotations (lines, arrows, text labels)."""

    def for_pin(self, pin) -> Self:
        """All markup items belonging to a specific parent pin."""
        return self.filter(parent_pin=pin)

    def for_wiki(self, wiki) -> Self:
        """All shared/community markup items belonging to a specific Wiki."""
        return self.filter(parent_wiki=wiki)

    def for_safety_checkin(self, safety_checkin) -> Self:
        """All markup items belonging to a specific safety check-in's route/plan."""
        return self.filter(parent_safety_checkin=safety_checkin)

    def for_profile(self, profile) -> Self:
        """All markup items belonging to a specific profile."""
        return self.filter(profile=profile)


class PinMarkupManager(abstract.FrontendDashboardManager.from_queryset(PinMarkupQuerySet)):
    """Manager for PinMarkup."""
