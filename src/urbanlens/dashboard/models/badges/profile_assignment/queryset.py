"""ProfileBadgeAssignment queryset and manager."""

from __future__ import annotations

from urbanlens.dashboard.models import abstract


class ProfileBadgeAssignmentQuerySet(abstract.QuerySet):
    """QuerySet for private user-badge assignments on profiles."""


class ProfileBadgeAssignmentManager(abstract.Manager.from_queryset(ProfileBadgeAssignmentQuerySet)):
    """Manager for ProfileBadgeAssignment records."""
