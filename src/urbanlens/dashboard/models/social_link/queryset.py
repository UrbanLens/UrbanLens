"""QuerySet and Manager for SocialLink."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.social_link.model import SocialLink  # noqa: F401 - mypy needs these; ruff does not


class SocialLinkQuerySet(abstract.DashboardQuerySet["SocialLink"]):
    """QuerySet for SocialLink."""


_SocialLinkManagerBase = abstract.DashboardManager.from_queryset(SocialLinkQuerySet)


class SocialLinkManager(_SocialLinkManagerBase["SocialLink"]):
    pass
