"""QuerySet and Manager for Comment."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from django.db.models import Exists, OuterRef

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


class CommentManager(abstract.FrontendDashboardManager.from_queryset(CommentQuerySet)):
    pass
