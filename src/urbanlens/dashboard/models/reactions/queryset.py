"""Custom queryset/manager for Reaction."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


class ReactionQuerySet(abstract.DashboardQuerySet):
    """Custom queryset for Reaction models."""

    def existing(self, profile: Profile, emoji: str, **target: object):
        """Find this profile's existing reaction with this emoji on one target, if any.

        A Reaction's target is polymorphic - exactly one of ``comment``,
        ``trip_comment``, or ``direct_message`` is ever set (see the model's
        own docstring and its per-target unique constraints) - so every
        caller toggling a reaction (comment/trip-comment/DM reaction views)
        needs the exact same "does this profile+emoji+target combo already
        exist" lookup, differing only in which target kwarg they pass.

        Args:
            profile: The reacting profile.
            emoji: The emoji being toggled.
            **target: Exactly one of ``comment=``, ``trip_comment=``, or
                ``direct_message=``, set to the target instance.

        Returns:
            The matching Reaction, or None.
        """
        return self.filter(profile=profile, emoji=emoji, **target).first()


class ReactionManager(abstract.DashboardManager.from_queryset(ReactionQuerySet)):
    """Custom query manager for Reaction models."""
