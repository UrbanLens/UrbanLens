"""FriendInvitation queryset and manager."""

from __future__ import annotations

from urbanlens.dashboard.models import abstract


class FriendInvitationQuerySet(abstract.QuerySet):
    """QuerySet for email-based friend invitations."""


class FriendInvitationManager(abstract.Manager.from_queryset(FriendInvitationQuerySet)):
    """Manager for FriendInvitation records."""
