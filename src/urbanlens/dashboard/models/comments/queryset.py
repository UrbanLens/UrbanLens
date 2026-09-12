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

    def visible_to(self, profile: Profile) -> Self:
        """Gates 1-3 of the comment visibility rules, entirely in SQL.

        The queryset counterpart to ``comments.comment_is_visible``, and held
        to it across the product of every visibility setting and every
        relationship by ``test_comment_visibility_is_a_queryset``. Gate 4
        (identity masking) is absent: it shapes how a surviving author is
        displayed, not whether a row is admitted.

        Gate 1 comes from ``Profile.visibility_permits_q``, which is exact
        rather than the superset ``Profile.related_profile_ids`` offers - a
        superset would be safe, but it leaves the gate to run again in Python
        after the page is cut, which is what makes a page come back short of
        the size it asked for.

        Answering these in SQL is what lets a caller page a comment list with
        LIMIT instead of building the whole thread and slicing the result.

        Args:
            profile: The viewing profile.

        Returns:
            The subset *profile* may see.
        """
        from urbanlens.dashboard.models.profile.model import Profile as ProfileModel

        # Gate 1 lives next to the predicate it mirrors, so the one rule has one
        # SQL form - trip comments ask the same question of the same field.
        author_permits = ProfileModel.visibility_permits_q(profile, author_path="profile", visibility_field="comment_visibility")
        # Gate 2: an image awaiting the async malware scan is visible only to
        # its own uploader.
        unscanned_and_not_mine = Q(pending_scan=True) & ~Q(profile_id=profile.pk)
        return self.filter(author_permits).exclude(unscanned_and_not_mine).mentions_all_visible_to(profile)


class CommentManager(abstract.FrontendDashboardManager.from_queryset(CommentQuerySet)):
    pass
