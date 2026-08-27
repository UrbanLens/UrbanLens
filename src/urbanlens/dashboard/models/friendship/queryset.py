from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Self

from django.db.models import Q

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus

if TYPE_CHECKING:
    from django.contrib.auth.models import User

    from urbanlens.dashboard.models.friendship.model import Friendship
    from urbanlens.dashboard.models.profile import Profile


logger = logging.getLogger(__name__)


class QuerySet(abstract.DashboardQuerySet["Friendship"]):
    def profile(self, profile: Profile | int) -> Self:
        """
        Return a list of all friendships for a given profile.
        """
        if isinstance(profile, int):
            return self.filter(
                Q(from_profile__id=profile) | Q(to_profile__id=profile),
            )

        return self.filter(
            Q(from_profile=profile) | Q(to_profile=profile),
        )

    def between(self, from_profile: Profile | int, to_profile: Profile | int) -> Friendship | None:
        """Return the relationship joining two profiles, in either direction.

        "One row per pair" is a convention (``Friendship.request`` reuses an
        existing row), not a constraint: ``unique_together`` is on
        ``(from_profile, to_profile)``, which permits ``A->B`` **and** ``B->A``
        to both exist. A profile import that restores both directions
        (``services.import_export.import_data``) or two simultaneous requests in
        opposite directions produce exactly that, and this used to ``.get()`` -
        so a reciprocal pair raised ``MultipleObjectsReturned`` out of the
        profile page, the friends API, and (once mute was wired into delivery)
        every notification between them.

        The oldest row wins, deterministically: it is the one the pair's history
        actually hangs off, and picking arbitrarily would make the answer depend
        on query planning. A second row is data to repair, not a reason to
        refuse to answer - see "reciprocal Friendship rows" in docs/PROBLEMS.md.

        Args:
            from_profile: One of the two profiles, or its pk.
            to_profile: The other, or its pk.

        Returns:
            The relationship, or None when the pair has none.
        """
        q1: dict[str, Any] = {}
        q2: dict[str, Any] = {}

        if isinstance(from_profile, int):
            q1["from_profile__id"] = from_profile
            q2["to_profile__id"] = from_profile
        else:
            q1["from_profile"] = from_profile
            q2["to_profile"] = from_profile

        if isinstance(to_profile, int):
            q1["to_profile__id"] = to_profile
            q2["from_profile__id"] = to_profile
        else:
            q1["to_profile"] = to_profile
            q2["from_profile"] = to_profile

        matches = list(self.filter(Q(**q1) | Q(**q2)).order_by("pk")[:2])
        if not matches:
            return None
        if len(matches) > 1:
            logger.warning("Two Friendship rows join profiles %s and %s (%s, %s); using the older one", from_profile, to_profile, matches[0].pk, matches[1].pk)
        return matches[0]

    def user(self, user: User) -> Self:
        """
        Return a list of all friendships for a given user.
        """
        return self.filter(
            Q(from_profile__user=user) | Q(to_profile__user=user),
        )

    def status(self, status: str) -> Self:
        """
        Return a list of all friendships with a given status.
        """
        return self.filter(status=status)

    def is_friend(self) -> Self:
        """
        Return a list of all friendships with a status of accepted.
        """
        return self.filter(status=FriendshipStatus.ACCEPTED)

    def not_friend(self) -> Self:
        """
        Return a list of all friendships with a status other than accepted.
        """
        return self.exclude(status=FriendshipStatus.ACCEPTED)

    def ever_friends(self) -> Self:
        """
        Return friendships that are (or once were) an accepted friendship.

        ``remove()`` never deletes the row, it just moves status to
        ``REMOVED`` - so this is the set of rows that reached ``ACCEPTED``
        at some point, unlike ``is_friend()`` which only sees the current
        state.
        """
        return self.filter(status__in=(FriendshipStatus.ACCEPTED, FriendshipStatus.REMOVED))

    def muted_by(self, viewer: Profile | int) -> Self:
        """Return the relationships ``viewer`` has muted.

        Reads the mute columns, never ``status``. Mute used to *be* a status,
        which is why this filter has to exist at all: any caller that reaches
        for ``status="Muted"`` is reproducing the bug where muting a friend
        overwrote ``Accepted`` and un-friended them everywhere.

        Takes the viewer because there is one row per pair with a column per
        side: "muted" is not a property of the relationship, and a filter that
        did not ask whose preference it meant could only answer the wrong
        question.

        Args:
            viewer: The profile whose own mutes to return, or its pk.

        Returns:
            The relationships that profile muted, whatever relationship state
            they are in.
        """
        viewer_id = viewer if isinstance(viewer, int) else viewer.pk
        return self.filter(Q(from_profile_id=viewer_id, muted_by_from_profile=True) | Q(to_profile_id=viewer_id, muted_by_to_profile=True))

    def not_muted_by(self, viewer: Profile | int) -> Self:
        """Return ``viewer``'s relationships whose notifications are still on.

        Args:
            viewer: The profile whose own mutes to exclude, or its pk.

        Returns:
            The relationships that profile has not muted. Relationships the
            profile is not part of are excluded too - the question only has an
            answer for their own rows.
        """
        viewer_id = viewer if isinstance(viewer, int) else viewer.pk
        return self.filter(Q(from_profile_id=viewer_id, muted_by_from_profile=False) | Q(to_profile_id=viewer_id, muted_by_to_profile=False))

    def relationship_type(self, relationship_type: str) -> Self:
        """
        Return a list of all friendships with a given type.
        """
        return self.filter(relationship_type=relationship_type)

    def has_permission(self, permission: str) -> Self:
        """
        Return a list of all friendships with a given permission.
        """
        return self.filter(permissions=permission)


class Manager(abstract.DashboardManager.from_queryset(QuerySet)):
    pass
