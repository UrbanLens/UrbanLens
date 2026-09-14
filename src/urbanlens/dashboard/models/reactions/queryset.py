"""Custom queryset/manager for Reaction."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.reactions.model import Reaction

#: The nullable foreign keys a ``Reaction`` may point at - the full set of "reactable" hosts.
REACTION_HOST_FIELDS: tuple[str, ...] = ("comment", "trip_comment", "direct_message", "group_message")


class ReactionQuerySet(abstract.DashboardQuerySet["Reaction"]):
    """Custom queryset for Reaction models."""

    def existing(self, profile: Profile, emoji: str, **target: Any) -> Reaction | None:
        """Find this profile's existing reaction with this emoji on one target, if any.
        A Reaction's target is polymorphic - exactly one of ``comment``, ``trip_comment``, ``direct_message`` or ``group_message`` is ever set (see the model's own docstring and its per-target unique constraints) - so every caller toggling a reaction (comment/trip-comment/DM/group-message reaction views) needs the exact same "does this profile+emoji+target combo already exist" lookup, differing only in which target kwarg they pass.

        Args:
            profile: The reacting profile.
            emoji: The emoji being toggled.
            **target: Exactly one of ``comment=``, ``trip_comment=``,
                ``direct_message=``, or ``group_message=``, set to the target
                instance.

        Returns:
            The matching Reaction, or None.

        Raises:
            ValueError: ``target`` did not name exactly one known host field.
        """
        hosts = [name for name in target if name in REACTION_HOST_FIELDS]
        if len(hosts) != 1 or len(target) != 1:
            raise ValueError(f"Reaction.objects.existing() needs exactly one of {REACTION_HOST_FIELDS}, got {sorted(target)}.")

        return self.filter(profile=profile, emoji=emoji, **target).first()


class ReactionManager(abstract.DashboardManager.from_queryset(ReactionQuerySet)):
    """Custom query manager for Reaction models."""
