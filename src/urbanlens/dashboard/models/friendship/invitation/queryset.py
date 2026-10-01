"""FriendInvitation queryset and manager."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.friendship.invitation.model import FriendInvitation  # noqa: F401 - mypy needs these; ruff does not


class FriendInvitationQuerySet(abstract.DashboardQuerySet["FriendInvitation"]):
    """QuerySet for email-based friend invitations."""


_FriendInvitationManagerBase = abstract.DashboardManager.from_queryset(FriendInvitationQuerySet)


class FriendInvitationManager(_FriendInvitationManagerBase):
    """Manager for FriendInvitation records."""
