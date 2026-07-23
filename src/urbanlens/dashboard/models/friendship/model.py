from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.core.validators import MaxLengthValidator
from django.db.models import CASCADE, CharField, ForeignKey, TextField

from urbanlens.dashboard.models.abstract import DashboardModel, TextChoices
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType, Permission
from urbanlens.dashboard.models.friendship.queryset import Manager
from urbanlens.dashboard.models.profile import Profile
from urbanlens.dashboard.services.text_limits import MAX_FRIEND_REQUEST_MESSAGE_LENGTH

logger = logging.getLogger(__name__)


class Friendship(DashboardModel):
    status = CharField(max_length=10, choices=FriendshipStatus.choices)
    relationship_type = CharField(max_length=12, choices=FriendshipType.choices)
    permissions = CharField(max_length=16, choices=Permission.choices)
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

    @classmethod
    def mute(cls, from_profile: Profile | int, to_profile: Profile | int) -> Friendship | None:
        """
        Mute a profile.
        """
        if friendship := cls.objects.all().between(from_profile, to_profile):
            friendship.status = FriendshipStatus.MUTED
            friendship.save()
            return friendship

        # Create a new friendship with status muted
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
            status=FriendshipStatus.MUTED,
        )

    def __str__(self):
        return f"{self.from_profile.username} to {self.to_profile.username} - {self.relationship_type} - {self.status}"

    class Meta(DashboardModel.Meta):
        db_table = "dashboard_friendships"
        unique_together = ("from_profile", "to_profile")
