"""QuerySet and Manager for SocialLink."""

from __future__ import annotations

from urbanlens.dashboard.models import abstract


class SocialLinkQuerySet(abstract.DashboardQuerySet):
    """QuerySet for SocialLink."""


class SocialLinkManager(abstract.DashboardManager.from_queryset(SocialLinkQuerySet)):
    pass
