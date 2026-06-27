"""Queryset and manager for PinMarkup."""

from __future__ import annotations

import logging
from typing import Self

from urbanlens.dashboard.models import abstract

logger = logging.getLogger(__name__)


class PinMarkupQuerySet(abstract.QuerySet):
    """QuerySet for PinMarkup map annotations (lines, arrows, text labels)."""

    def for_pin(self, pin) -> Self:
        """All markup items belonging to a specific parent pin."""
        return self.filter(parent_pin=pin)

    def for_profile(self, profile) -> Self:
        """All markup items belonging to a specific profile."""
        return self.filter(profile=profile)


class PinMarkupManager(abstract.Manager.from_queryset(PinMarkupQuerySet)):
    """Manager for PinMarkup."""
