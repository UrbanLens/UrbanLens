"""QuerySet and Manager for Comment."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from django.db.models import Exists, OuterRef, Q

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki


class CommentQuerySet(abstract.FrontendDashboardQuerySet):
    def top_level(self) -> Self:
        """Return only top-level comments (not replies)."""
        return self.filter(parent__isnull=True)

    def for_pin(self, pin: Pin) -> Self:
        return self.filter(pin=pin, parent__isnull=True)

    def for_wiki(self, wiki: Wiki) -> Self:
        return self.filter(wiki=wiki, parent__isnull=True)

    def mentions_all_visible_to(self, profile: Profile) -> Self:
        """Drop comments naming a location *profile* has not pinned.

        Gate 3 of the comment visibility rules, as SQL - see
        ``services.comments.comments`` for what the gate is for and
        ``models.comments.location_mention`` for where the rows come from.
        Comments naming nothing pass, which is the overwhelming majority.

        The viewer's pins stay a subquery rather than a materialised uuid set:
        the cost of reading a comment list should not grow with how many pins
        the reader happens to have.

        Args:
            profile: The viewing profile.

        Returns:
            The subset whose every named location the viewer has pinned.
        """
        from urbanlens.dashboard.models.comments.location_mention import CommentLocationMention
        from urbanlens.dashboard.models.pin.model import Pin

        pinned = Pin.objects.filter(profile=profile).exclude(location__isnull=True).values("location__uuid")
        unpinned_mention = CommentLocationMention.objects.filter(comment_id=OuterRef("pk")).exclude(location_uuid__in=pinned)
        return self.filter(~Exists(unpinned_mention))

    def _author_permits(self, profile: Profile) -> Q:
        """Gate 1 as SQL: the author's ``comment_visibility``, from the viewer's side.

        Mirrors ``Profile.visibility_permits`` branch for branch, including its
        ordering - NO_ONE is refused before a friendship is considered, and a
        friendship (or an unanswered request the author sent the viewer) passes
        every other setting. ``comment_is_visible``'s own short-circuit on
        ``self == author`` is the first term.

        Every relationship stays a subquery: the viewer's friends, places,
        mutual friends and trips are their own data, and pulling them into
        Python to build an ``IN`` list would make reading a comment list cost
        more for a viewer with more of it.

        Deliberately exact rather than the superset
        ``Profile.related_profile_ids`` offers. A superset would be safe, but
        it leaves gate 1 to run again in Python after the page is cut, which is
        what makes a page come back short of what it asked for.

        Args:
            profile: The viewing profile.

        Returns:
            A ``Q`` admitting exactly the comments whose author permits *profile*.
        """
        from urbanlens.dashboard.models.friendship.meta import FriendshipStatus
        from urbanlens.dashboard.models.friendship.model import Friendship
        from urbanlens.dashboard.models.pin.model import Pin
        from urbanlens.dashboard.models.profile.meta import VisibilityChoice
        from urbanlens.dashboard.models.trips.model import TripMembership

        accepted = FriendshipStatus.ACCEPTED
        friends_out = Friendship.objects.filter(from_profile=profile, status=accepted).values("to_profile_id")
        friends_in = Friendship.objects.filter(to_profile=profile, status=accepted).values("from_profile_id")
        # One way, matching has_pending_request_to(author, viewer): asking to
        # connect opens the asker's own gates to the person being asked.
        askers = Friendship.objects.filter(to_profile=profile, status__in=(FriendshipStatus.REQUESTED, FriendshipStatus.PENDING)).values("from_profile_id")
        connected = Q(profile_id__in=friends_out) | Q(profile_id__in=friends_in) | Q(profile_id__in=askers)

        # Place-keyed where a pin has a place, exact Location otherwise - the
        # SQL form of services.pins.common_pins.pinned_place_keys, so two pins
        # fifty metres apart on one parcel count as shared.
        viewer_pins = Pin.objects.filter(profile=profile, location__isnull=False)
        common_pin = Q(
            profile_id__in=Pin.objects.filter(
                Q(location__place_id__in=viewer_pins.exclude(location__place__isnull=True).values("location__place_id")) | Q(location_id__in=viewer_pins.filter(location__place__isnull=True).values("location_id")),
            ).values("profile_id"),
        )

        common_friend = Q()
        for mine in (friends_out, friends_in):
            common_friend |= Q(profile_id__in=Friendship.objects.filter(status=accepted, to_profile_id__in=mine).values("from_profile_id"))
            common_friend |= Q(profile_id__in=Friendship.objects.filter(status=accepted, from_profile_id__in=mine).values("to_profile_id"))

        viewer_trips = TripMembership.objects.filter(profile=profile).values("trip_id")
        common_trip = Q(profile_id__in=TripMembership.objects.filter(trip_id__in=viewer_trips).values("profile_id"))

        anything = VisibilityChoice.ANYTHING_IN_COMMON
        return (
            Q(profile_id=profile.pk)
            | Q(profile__comment_visibility=VisibilityChoice.ANYONE)
            | (~Q(profile__comment_visibility=VisibilityChoice.NO_ONE) & connected)
            | (Q(profile__comment_visibility__in=(VisibilityChoice.COMMON_PIN, anything)) & common_pin)
            | (Q(profile__comment_visibility__in=(VisibilityChoice.COMMON_FRIEND, anything)) & common_friend)
            | (Q(profile__comment_visibility__in=(VisibilityChoice.COMMON_TRIP, anything)) & common_trip)
        )

    def visible_to(self, profile: Profile) -> Self:
        """Gates 1-3 of the comment visibility rules, entirely in SQL.

        The queryset counterpart to ``comments.comment_is_visible``, and held
        to it across the product of every visibility setting and every
        relationship by ``test_comment_visibility_is_a_queryset``. Gate 4
        (identity masking) is absent: it shapes how a surviving author is
        displayed, not whether a row is admitted.

        Answering these in SQL is what lets a caller page a comment list with
        LIMIT instead of building the whole thread and slicing the result.

        Args:
            profile: The viewing profile.

        Returns:
            The subset *profile* may see.
        """
        # Gate 2: an image awaiting the async malware scan is visible only to
        # its own uploader.
        unscanned_and_not_mine = Q(pending_scan=True) & ~Q(profile_id=profile.pk)
        return self.filter(self._author_permits(profile)).exclude(unscanned_and_not_mine).mentions_all_visible_to(profile)


class CommentManager(abstract.FrontendDashboardManager.from_queryset(CommentQuerySet)):
    pass
