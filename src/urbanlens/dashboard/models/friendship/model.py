from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.core.validators import MaxLengthValidator
from django.db import IntegrityError, transaction
from django.db.models import CASCADE, BooleanField, CharField, ForeignKey, TextField, UniqueConstraint
from django.db.models.functions import Greatest, Least

from urbanlens.dashboard.models.abstract import DashboardModel
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType, Permission
from urbanlens.dashboard.models.friendship.queryset import Manager
from urbanlens.dashboard.models.profile import Profile
from urbanlens.dashboard.services.core.text_limits import MAX_FRIEND_REQUEST_MESSAGE_LENGTH

logger = logging.getLogger(__name__)


class Friendship(DashboardModel):
    """One directional relationship row between two profiles.

    ``status`` is the relationship; mute columns are per-side notification
    preference and must stay separate from it.
    """

    status = CharField(max_length=10, choices=FriendshipStatus.choices)
    # Per-side mute preference; read via is_muted_by.
    muted_by_from_profile = BooleanField(default=False)
    muted_by_to_profile = BooleanField(default=False)
    relationship_type = CharField(max_length=12, choices=FriendshipType.choices)
    permissions = CharField(max_length=16, choices=Permission.choices, default=Permission.VIEW_PROFILE)
    # Optional note attached at request creation.
    request_message = TextField(
        null=True,
        blank=True,
        max_length=MAX_FRIEND_REQUEST_MESSAGE_LENGTH,
        validators=[MaxLengthValidator(MAX_FRIEND_REQUEST_MESSAGE_LENGTH)],
    )

    from_profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="friendships",
    )
    to_profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="friends_to_me",
    )

    if TYPE_CHECKING:
        from_profile_id: int
        to_profile_id: int

    objects = Manager()

    @classmethod
    def request(
        cls,
        from_profile: Profile | int,
        to_profile: Profile | int,
        relationship_type: str = FriendshipType.FRIEND,
        message: str | None = None,
    ) -> Friendship | None:
        """
        Create a new friendship request.

        Args:
            from_profile: Profile sending the request.
            to_profile: Profile being requested.
            relationship_type: Requested relationship tier.
            message: Optional note from the requester, stored on the row and
                surfaced in the recipient's notification.
        """
        if isinstance(from_profile, int):
            from_profile = Profile.objects.get(pk=from_profile)
        if isinstance(to_profile, int):
            to_profile = Profile.objects.get(pk=to_profile)

        if not from_profile or not to_profile:
            logger.warning("Could not find profiles")
            raise ValueError("Could not find profiles")

        # guaranteed above, but handle case in the event code drifts.
        if not isinstance(from_profile, Profile) or not isinstance(to_profile, Profile):
            raise TypeError("Could not find profiles")

        # A profile with Community turned off can neither send nor be sent
        # friend requests - checked here since this is the one chokepoint
        # every request path (button click, invite acceptance, pending
        # invitation auto-accept) routes through.
        if not from_profile.community_enabled or not to_profile.community_enabled:
            logger.info("Friendship request blocked: Community disabled for from=%s or to=%s", from_profile.pk, to_profile.pk)
            return None

        # Check if a request has already been made
        if friendship := cls.objects.all().between(from_profile, to_profile):
            # Check if we can make another request
            if not FriendshipStatus.can_request(friendship.status):
                logger.warning("Cannot request another friendship")
                return None

            # Re-orient the row so the direction matches who asked.
            if friendship.from_profile_id != from_profile.pk:
                # Prefer an existing forward row; legacy DBs may hold both directions.
                forward = cls.objects.filter(from_profile=from_profile, to_profile=to_profile).first()
                if forward is None:
                    friendship.from_profile = from_profile
                    friendship.to_profile = to_profile
                    # Mute flags are positional, so they travel with the ends.
                    friendship.muted_by_from_profile, friendship.muted_by_to_profile = (
                        friendship.muted_by_to_profile,
                        friendship.muted_by_from_profile,
                    )
                elif FriendshipStatus.can_request(forward.status):
                    friendship = forward
                else:
                    logger.warning("Cannot request another friendship: reciprocal row is %s", forward.status)
                    return None

            friendship.status = FriendshipStatus.REQUESTED
            friendship.request_message = message
            friendship.save(
                update_fields=[
                    "from_profile",
                    "to_profile",
                    "muted_by_from_profile",
                    "muted_by_to_profile",
                    "status",
                    "request_message",
                    "updated",
                ]
            )
        else:
            try:
                # Savepoint keeps a failed insert from poisoning the transaction.
                with transaction.atomic():
                    friendship = cls.objects.create(
                        from_profile=from_profile,
                        to_profile=to_profile,
                        relationship_type=relationship_type,
                        status=FriendshipStatus.REQUESTED,
                        request_message=message,
                    )
            except IntegrityError:
                # Concurrent opposite request won; return that row.
                logger.info("Friendship request from %s to %s lost the race; returning the row that won", from_profile.pk, to_profile.pk)
                friendship = cls.objects.all().between(from_profile, to_profile)
                # Guard against the winner being a non-requestable state.
                if friendship is not None and not FriendshipStatus.can_request(friendship.status) and friendship.status != FriendshipStatus.REQUESTED:
                    logger.warning("Cannot request another friendship: the row that won is %s", friendship.status)
                    return None

        return friendship

    @staticmethod
    def profile_at_max_friends(profile: Profile) -> bool:
        """Whether ``profile`` already reached its max-friends limit."""
        from urbanlens.dashboard.models.site_settings.model import SiteSettings

        max_friends = SiteSettings.get_current().max_friends_per_user
        if max_friends <= 0:
            return False
        return Friendship.objects.profile(profile).is_friend().count() >= max_friends

    def accept(self) -> bool:
        """Accept the request; no-op when Community is off or friends are maxed."""
        if not self.from_profile.community_enabled or not self.to_profile.community_enabled:
            logger.info("Friendship accept blocked: Community disabled for from=%s or to=%s", self.from_profile_id, self.to_profile_id)
            return False

        for profile in (self.from_profile, self.to_profile):
            if Friendship.profile_at_max_friends(profile):
                logger.info("Friendship accept blocked: profile=%s already at max_friends_per_user", profile.pk)
                return False

        self._set_status(FriendshipStatus.ACCEPTED)
        return True

    def _set_status(self, status: str) -> None:
        """Write one status transition; keeps mute columns and signals intact."""
        self.status = status
        self.save(update_fields=["status", "updated"])

    def decline(self):
        """Decline a friendship request (requester can re-send later)."""
        self._set_status(FriendshipStatus.DECLINED)

    def ignore(self):
        """Ignore a friendship request (requester cannot re-send; no notification sent)."""
        self._set_status(FriendshipStatus.IGNORED)

    def remove(self):
        """
        Remove a friendship.
        """
        self._set_status(FriendshipStatus.REMOVED)

    @classmethod
    def block(cls, from_profile: Profile | int, to_profile: Profile | int) -> Friendship | None:
        """
        Block a profile.
        """
        if friendship := cls.objects.all().between(from_profile, to_profile):
            friendship._set_status(FriendshipStatus.BLOCKED)  # noqa: SLF001 - same class
            return friendship

        # Create a new friendship with status blocked
        if isinstance(from_profile, int):
            from_profile = Profile.objects.get(pk=from_profile)
        if isinstance(to_profile, int):
            to_profile = Profile.objects.get(pk=to_profile)

        if not from_profile or not to_profile:
            logger.warning("Could not find profiles")
            raise ValueError("Could not find profiles")

        return cls.objects.create(
            from_profile=from_profile,
            to_profile=to_profile,
            status=FriendshipStatus.BLOCKED,
        )

    def _mute_field_for(self, viewer: Profile | int) -> str:
        """Name the mute column belonging to ``viewer``.

        Args:
            viewer: The profile whose own preference is being read or written,
                or its pk.

        Returns:
            ``"muted_by_from_profile"`` or ``"muted_by_to_profile"``.

        Raises:
            ValueError: ``viewer`` is not one of this row's two profiles.
                Raised rather than defaulted, because every wrong answer here
                silences somebody who did not ask to be silenced.
        """
        viewer_id = viewer if isinstance(viewer, int) else viewer.pk
        if viewer_id == self.from_profile_id:
            return "muted_by_from_profile"
        if viewer_id == self.to_profile_id:
            return "muted_by_to_profile"
        raise ValueError(f"Profile {viewer_id} is not part of friendship {self.pk}")

    def is_muted_by(self, viewer: Profile | int) -> bool:
        """Whether ``viewer`` has silenced notifications from the other side.

        Args:
            viewer: The profile whose own preference to read, or its pk.

        Returns:
            True when that profile muted this relationship.

        Raises:
            ValueError: ``viewer`` is not part of this relationship.
        """
        return bool(getattr(self, self._mute_field_for(viewer)))

    def mute(self, viewer: Profile | int) -> None:
        """Silence notifications ``viewer`` would receive from the other side.

        An instance method rather than the ``(from_profile, to_profile)``
        classmethod it replaces, for two reasons. First, it now sits alongside
        :meth:`accept`/:meth:`decline`/:meth:`ignore`/:meth:`remove` as one
        more transition on an existing row, which is what it always was.
        Second, the old classmethod *created* a ``Muted`` row when the two
        profiles had no relationship at all - inventing a relationship out of
        nothing in order to record a preference about it, and (since ``Muted``
        was a status) simultaneously making the pair permanently unable to
        send each other a friend request. Muting a stranger is meaningless;
        there is nothing to turn the volume down on.

        Written as a targeted ``UPDATE`` rather than ``save()`` for three
        reasons. It cannot clobber a concurrent accept/decline of the same
        row, since no other column is in the statement; it cannot clobber the
        *other* side's mute preference, which a full save of a stale instance
        would; and it leaves ``updated`` alone. ``updated`` is ``auto_now``,
        and the profile page renders it as the friendship's "since" date - a
        notification preference must not rewrite when two people became
        friends.

        Args:
            viewer: The profile doing the muting, or its pk.

        Raises:
            ValueError: ``viewer`` is not part of this relationship.
        """
        self._set_muted(viewer, muted=True)

    def unmute(self, viewer: Profile | int) -> None:
        """Restore notifications ``viewer`` had silenced.

        The exact inverse of :meth:`mute`. Under the old status-based scheme
        there was no inverse to write: the pre-mute status had been discarded,
        so the profile page's "Unmute" button posted to the friend-request
        endpoint instead and was rejected outright, because
        ``FriendshipStatus.can_request`` excludes ``Muted``.

        Args:
            viewer: The profile doing the unmuting, or its pk.

        Raises:
            ValueError: ``viewer`` is not part of this relationship.
        """
        self._set_muted(viewer, muted=False)

    def _set_muted(self, viewer: Profile | int, *, muted: bool) -> None:
        """Drive one side's mute flag to ``muted``, idempotently."""
        field = self._mute_field_for(viewer)
        if bool(getattr(self, field)) == muted:
            return
        Friendship.objects.filter(pk=self.pk).update(**{field: muted})
        setattr(self, field, muted)

    def __str__(self):
        return f"{self.from_profile.username} to {self.to_profile.username} - {self.relationship_type} - {self.status}"

    class Meta(DashboardModel.Meta):
        db_table = "dashboard_friendships"
        unique_together = ("from_profile", "to_profile")
        constraints = [
            # "One row per pair" was a convention every reader relied on and
            # nothing enforced: `unique_together` stops a duplicate in one
            # direction and permits `A->B` *and* `B->A`. A profile import
            # restoring both, or two simultaneous requests in opposite
            # directions, produced exactly that - and `between()` then had two
            # rows to choose between, with the mute columns split across them.
            #
            # Expressed on the *ordered pair* rather than by reordering the
            # columns, which was the other candidate: `from_profile` means "who
            # asked", which `Pending`/`Requested` and `request_message` depend
            # on, so normalising the columns to id order would invert that
            # meaning for half the table. This gets the same guarantee and
            # leaves the direction alone.
            UniqueConstraint(
                Least("from_profile_id", "to_profile_id"),
                Greatest("from_profile_id", "to_profile_id"),
                name="friendship_one_row_per_pair",
            ),
        ]
