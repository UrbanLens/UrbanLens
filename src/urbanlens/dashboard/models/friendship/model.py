from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.core.validators import MaxLengthValidator
from django.db import IntegrityError, transaction
from django.db.models import CASCADE, BooleanField, CharField, ForeignKey, TextField, UniqueConstraint
from django.db.models.functions import Greatest, Least

from urbanlens.dashboard.models.abstract import DashboardModel, TextChoices
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType, Permission
from urbanlens.dashboard.models.friendship.queryset import Manager
from urbanlens.dashboard.models.profile import Profile
from urbanlens.dashboard.services.core.text_limits import MAX_FRIEND_REQUEST_MESSAGE_LENGTH

logger = logging.getLogger(__name__)


class Friendship(DashboardModel):
    """One directional relationship row between two profiles.

    ``status`` answers *what kind of relationship is this* and is the single
    thing every visibility gate reads (through ``Profile.are_friends``, which
    matches ``ACCEPTED`` and nothing else). The mute columns answer the entirely
    separate question *do I want to hear from them*. Those two facts must never
    share a column again: mute used to be a ``FriendshipStatus`` value, so
    muting an accepted friend overwrote ``Accepted`` and thereby revoked the
    friendship for every downstream permission check - profile fields, pin
    visibility, direct messages, common-pin/common-trip queries - while also
    making the relationship unrecoverable, since the pre-mute status was not
    stored anywhere and ``FriendshipStatus.can_request`` refuses ``Muted``.

    Mute is per-side (:meth:`is_muted_by`, :meth:`mute`, :meth:`unmute`) even
    though the row is shared, and
    ``services.social.friendship.notifications_muted`` is what turns the
    preference into actual silence.
    """

    status = CharField(max_length=10, choices=FriendshipStatus.choices)
    # Notification volume control, deliberately orthogonal to ``status`` - see
    # the class docstring for the data-integrity bug that separating them
    # fixes. Muting is not a relationship state: a muted friend is still a
    # friend, and unmuting must be able to return the pair to exactly where
    # they were, which is only possible if the relationship state was never
    # overwritten in the first place.
    #
    # One column per side, because a pair normally has exactly one row
    # (``between()`` matches either direction and ``request()`` reuses the
    # existing row) and mute is a preference of one *person*, not of the
    # relationship. "Normally" was doing real work until 2026-09-05:
    # ``unique_together`` did not forbid ``A->B`` and ``B->A`` both existing,
    # and ``friendship_one_row_per_pair`` now does. Anything asking "did X mute
    # Y" still reads *either* row rather than one - which is what
    # ``services.social.friendship.notifications_muted`` does - because a
    # database restored from before that migration can still hold the pair. A single shared boolean - which is what this was, inherited
    # from the ``status='Muted'`` encoding it replaced - meant A muting B also
    # read as muted from B's side, so wiring it into delivery would have
    # silenced the wrong person. Read these through :meth:`is_muted_by` rather
    # than directly; which column belongs to a viewer depends on which end of
    # the row they are, and that is exactly the detail a caller gets wrong.
    muted_by_from_profile = BooleanField(default=False)
    muted_by_to_profile = BooleanField(default=False)
    relationship_type = CharField(max_length=12, choices=FriendshipType.choices)
    # No production code path ever set this explicitly, so every row used to
    # persist "" (not a valid Permission choice) and has_permission() was
    # effectively dead against real data. VIEW_PROFILE is the value every
    # test fixture/baker recipe already uses as the baseline permission for a
    # friendship (see baker_recipes.friendship/accepted_friendship) - the
    # weakest capability, matching how has_permission() behaved in practice
    # (no real permission was ever actually granted, just invalidly stored).
    permissions = CharField(max_length=16, choices=Permission.choices, default=Permission.VIEW_PROFILE)
    # Optional note the requester attached when the request was first sent.
    # Only ever set on creation - never touched by accept()/decline()/etc.
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

            # Re-orient the row before reviving it. Without this it keeps the
            # ends it had when it was declined or removed, so B re-adding A is
            # recorded as though *A* had asked - and the person it was actually
            # sent to cannot accept it, because from their side there is no
            # incoming request. Both people see a request neither can act on.
            # See "re-adding a removed friend" in docs/PROBLEMS.md.
            if friendship.from_profile_id != from_profile.pk:
                # A row pointing the right way is preferred over swapping this
                # one's ends. Since 2026-09-05 a pair can only have one row
                # (`friendship_one_row_per_pair`), so `forward` and `friendship`
                # are the same row whenever both exist - the branch is kept
                # because a database predating that constraint can still hold
                # the pair until its migration runs.
                forward = cls.objects.filter(from_profile=from_profile, to_profile=to_profile).first()
                if forward is None:
                    friendship.from_profile = from_profile
                    friendship.to_profile = to_profile
                    # These two are *positional* - which column belongs to a
                    # viewer depends on which end of the row they are - so they
                    # have to travel with the ends, or A's mute silently
                    # becomes B's.
                    friendship.muted_by_from_profile, friendship.muted_by_to_profile = (
                        friendship.muted_by_to_profile,
                        friendship.muted_by_from_profile,
                    )
                elif FriendshipStatus.can_request(forward.status):
                    friendship = forward
                else:
                    logger.warning("Cannot request another friendship: reciprocal row is %s", forward.status)
                    return None

            # Update the status to requested
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
                # The savepoint is load-bearing, not defensive. A failed insert
                # marks the whole transaction unusable in Postgres, so without
                # it the re-read below raises TransactionManagementError instead
                # of answering - and so would everything else the caller went on
                # to do.
                with transaction.atomic():
                    friendship = cls.objects.create(
                        from_profile=from_profile,
                        to_profile=to_profile,
                        relationship_type=relationship_type,
                        status=FriendshipStatus.REQUESTED,
                        request_message=message,
                    )
            except IntegrityError:
                # Two opposite requests at once: `between()` above saw nothing,
                # and the other one inserted first. Before the pair constraint
                # this produced two rows for one relationship, with the mute
                # columns split across them; now it is a refused insert, and the
                # right answer is the row that won - the two people wanted the
                # same thing, and the loser's request is satisfied by it.
                logger.info("Friendship request from %s to %s lost the race; returning the row that won", from_profile.pk, to_profile.pk)
                friendship = cls.objects.all().between(from_profile, to_profile)
                # The same guard the found-row branch applies. The winner is
                # usually the mirror request, but it can be a BLOCKED or IGNORED
                # row written in the same window - and returning that would have
                # the caller report a request it never made.
                if friendship is not None and not FriendshipStatus.can_request(friendship.status) and friendship.status != FriendshipStatus.REQUESTED:
                    logger.warning("Cannot request another friendship: the row that won is %s", friendship.status)
                    return None

        return friendship

    @staticmethod
    def profile_at_max_friends(profile: Profile) -> bool:
        """Return whether ``profile`` is already at the site's max-friends limit.

        Args:
            profile: Profile to check.

        Returns:
            True when the site's ``max_friends_per_user`` is set (non-zero)
            and ``profile`` already has that many accepted friends.
        """
        from urbanlens.dashboard.models.site_settings.model import SiteSettings

        max_friends = SiteSettings.get_current().max_friends_per_user
        if max_friends <= 0:
            return False
        return Friendship.objects.profile(profile).is_friend().count() >= max_friends

    def accept(self) -> bool:
        """Accept a friendship request.

        Returns:
            True if accepted, False (no-op) if either profile has Community
            disabled - accepting would create a mutual, visible friendship,
            which a Community-disabled profile cannot have - or if either
            profile is already at the site's max-friends limit.
        """
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
        """Write one status transition, and nothing else.

        ``update_fields`` rather than a bare ``save()``: a bare save writes
        every column from this in-memory instance, which is only correct when
        nothing else has touched the row since it was loaded. The mute columns
        are written by a targeted ``UPDATE`` that leaves the instance alone
        (see :meth:`_set_muted`), so an instance loaded before somebody muted
        and saved after it would silently un-mute them - and a mute is a
        preference the person set deliberately, restored by nobody.

        Not a ``queryset.update()``, which would avoid the problem outright but
        also skip ``post_save`` - and the achievements system subscribes to it
        for this model, specifically to see a friendship *reach* ``ACCEPTED``
        (``models.achievements.signals``, ``created_only=False``). Silencing
        that signal to fix a lost update would trade one silent bug for
        another.

        ``updated`` is included deliberately: it is ``auto_now``, and the
        profile page renders it as the friendship's "since" date, which a
        status transition legitimately moves. Mute deliberately does not.

        Args:
            status: The ``FriendshipStatus`` to move to.
        """
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
