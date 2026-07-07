"""FriendInvitation queryset and manager."""

from __future__ import annotations

from urbanlens.dashboard.models import abstract


class FriendInvitationQuerySet(abstract.DashboardQuerySet):
    """QuerySet for email-based friend invitations."""


class FriendInvitationManager(abstract.DashboardManager.from_queryset(FriendInvitationQuerySet)):
    """Manager for FriendInvitation records."""
