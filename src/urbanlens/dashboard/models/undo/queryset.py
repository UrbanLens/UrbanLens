"""QuerySet/Manager for UndoAction."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.utils import timezone

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.undo.model import UndoAction


class UndoActionQuerySet(abstract.FrontendDashboardQuerySet["UndoAction"]):
    """QuerySet for UndoAction, scoped by owning profile and retention window."""

    def for_profile(self, profile: Profile) -> UndoActionQuerySet:
        """Restrict to undo actions owned by ``profile``."""
        return self.filter(profile=profile)

    def active(self) -> UndoActionQuerySet:
        """Restrict to undo actions still within their retention window."""
        from urbanlens.dashboard.models.undo.model import UNDO_RETENTION

        return self.filter(created__gte=timezone.now() - UNDO_RETENTION)

    def expired(self) -> UndoActionQuerySet:
        """Restrict to undo actions past their retention window (cache entry already gone)."""
        from urbanlens.dashboard.models.undo.model import UNDO_RETENTION

        return self.filter(created__lt=timezone.now() - UNDO_RETENTION)


class UndoActionManager(abstract.FrontendDashboardManager.from_queryset(UndoActionQuerySet)):
    """Manager for UndoAction."""
