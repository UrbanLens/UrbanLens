from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.core.validators import MaxLengthValidator
from django.db.models import CASCADE, BooleanField, CharField, ForeignKey, TextField

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
    matches ``ACCEPTED`` and nothing else). ``muted`` answers the entirely
    separate question *do I want to hear from them*. Those two facts must never
    share a column again: mute used to be a ``FriendshipStatus`` value, so
    muting an accepted friend overwrote ``Accepted`` and thereby revoked the
    friendship for every downstream permission check - profile fields, pin
    visibility, direct messages, common-pin/common-trip queries - while also
    making the relationship unrecoverable, since the pre-mute status was not
    stored anywhere and ``FriendshipStatus.can_request`` refuses ``Muted``.
    """

    status = CharField(max_length=10, choices=FriendshipStatus.choices)
    # Notification volume control, deliberately orthogonal to ``status`` - see
    # the class docstring for the data-integrity bug that separating them
    # fixes. Muting is not a relationship state: a muted friend is still a
    # friend, and unmuting must be able to return the pair to exactly where
    # they were, which is only possible if the relationship state was never
    # overwritten in the first place.
    #
    # KNOWN LIMITATION, inherited unchanged from the status-based encoding this
    # replaces: there is exactly one Friendship row per pair (``between()``
    # matches either direction and ``request()`` reuses the existing row), so
    # this flag is shared by both profiles rather than per-viewer. If A mutes
    # B, B's own view of the relationship reads as muted too. The correctly
    # shaped model for this is ``DirectMessageMute``, which is keyed on
    # (viewer, sender); fixing it here means either two columns or a separate
    # row per direction, and is tracked in "`Friendship.muted` is shared by
    # both profiles, not per-viewer" in docs/PROBLEMS.md - see also "`Friendship.muted`
    # is stored but nothing reads it" there, which is the more immediate problem. Anything
    # surfacing this as "muted by me" is wrong today.
    muted = BooleanField(default=False)
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

            # Update the status to requested
            friendship.status = FriendshipStatus.REQUESTED
            friendship.request_message = message

        else:
            friendship = cls.objects.create(
                from_profile=from_profile,
                to_profile=to_profile,
                relationship_type=relationship_type,
                status=FriendshipStatus.REQUESTED,
                request_message=message,
            )

        friendship.save()
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

        self.status = FriendshipStatus.ACCEPTED
        self.save()
        return True

    def decline(self):
        """Decline a friendship request (requester can re-send later)."""
        self.status = FriendshipStatus.DECLINED
        self.save()

    def ignore(self):
        """Ignore a friendship request (requester cannot re-send; no notification sent)."""
        self.status = FriendshipStatus.IGNORED
        self.save()

    def remove(self):
        """
        Remove a friendship.
        """
        self.status = FriendshipStatus.REMOVED
        self.save()

    @classmethod
    def block(cls, from_profile: Profile | int, to_profile: Profile | int) -> Friendship | None:
        """
        Block a profile.
        """
        if friendship := cls.objects.all().between(from_profile, to_profile):
            friendship.status = FriendshipStatus.BLOCKED
            friendship.save()
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

    def mute(self) -> None:
        """Silence notifications from this relationship, without changing it.

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

        Written as a targeted ``UPDATE`` rather than ``save()`` for two
        reasons. It cannot clobber a concurrent accept/decline of the same
        row, since no other column is in the statement; and it leaves
        ``updated`` alone. ``updated`` is ``auto_now``, and the profile page
        renders it as the friendship's "since" date - a notification
        preference must not rewrite when two people became friends.
        """
        if self.muted:
            return
        Friendship.objects.filter(pk=self.pk).update(muted=True)
        self.muted = True

    def unmute(self) -> None:
        """Restore notifications from this relationship.

        The exact inverse of :meth:`mute`. Under the old status-based scheme
        there was no inverse to write: the pre-mute status had been discarded,
        so the profile page's "Unmute" button posted to the friend-request
        endpoint instead and was rejected outright, because
        ``FriendshipStatus.can_request`` excludes ``Muted``.
        """
        if not self.muted:
            return
        Friendship.objects.filter(pk=self.pk).update(muted=False)
        self.muted = False

    def __str__(self):
        return f"{self.from_profile.username} to {self.to_profile.username} - {self.relationship_type} - {self.status}"

    class Meta(DashboardModel.Meta):
        db_table = "dashboard_friendships"
        unique_together = ("from_profile", "to_profile")
