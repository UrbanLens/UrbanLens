"""What a block hides between two people who share a group chat or a trip.

A block removes neither person from a space they share. Each stops seeing the other there: the other's
presence at once, and whatever the other posts from the moment the block was placed. What either posted
before the block stays visible to the other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from django.db.models import Q

from urbanlens.dashboard.models.friendship.meta import FriendshipStatus

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from datetime import datetime

    from django.db.models import QuerySet

    from urbanlens.dashboard.models.profile.model import Profile


@dataclass(frozen=True, slots=True)
class SharedSpaceBlocks:
    """One profile's blocks, resolved in one query, for hiding each blocked pair from each other in a shared space.

    Symmetric: the blocker and the blocked are hidden from each other alike, so the same instance answers
    "may this viewer see that author's content" and "may this sender's content reach that recipient".

    Attributes:
        viewer_id: The profile whose view this describes.
        since: For every profile in a block with the viewer, in either direction, when that block was placed.
    """

    viewer_id: int
    since: Mapping[int, datetime] = field(default_factory=dict)

    @classmethod
    def for_viewer(cls, viewer: Profile | int, *, among: Iterable[int] | None = None) -> SharedSpaceBlocks:
        """Resolve one profile's blocks.

        Args:
            viewer: The profile, or its pk.
            among: When given, only blocks with these profiles are resolved.

        Returns:
            The viewer's blocks.
        """
        viewer_id = viewer if isinstance(viewer, int) else viewer.pk
        if among is None:
            pair = Q(from_profile_id=viewer_id) | Q(to_profile_id=viewer_id)
        else:
            others = set(among)
            if not others:
                return cls(viewer_id)
            pair = Q(from_profile_id=viewer_id, to_profile_id__in=others) | Q(to_profile_id=viewer_id, from_profile_id__in=others)
        since: dict[int, datetime] = {}
        for from_id, to_id, blocked_at in _blocked_rows(pair):
            since[to_id if from_id == viewer_id else from_id] = blocked_at
        return cls(viewer_id, since)

    @classmethod
    def for_profiles(cls, profile_ids: Iterable[int]) -> dict[int, SharedSpaceBlocks]:
        """Resolve the blocks among a set of profiles - a group's or a trip's members - in one query.

        Args:
            profile_ids: The profiles sharing the space.

        Returns:
            Each profile's blocks with the others in the set, keyed by pk.
        """
        ids = set(profile_ids)
        since: dict[int, dict[int, datetime]] = {pk: {} for pk in ids}
        if len(ids) > 1:
            for from_id, to_id, blocked_at in _blocked_rows(Q(from_profile_id__in=ids, to_profile_id__in=ids)):
                since[from_id][to_id] = blocked_at
                since[to_id][from_id] = blocked_at
        return {pk: cls(pk, others) for pk, others in since.items()}

    @property
    def hidden_profile_ids(self) -> frozenset[int]:
        """Everyone the viewer and they are hidden from each other."""
        return frozenset(self.since)

    def hides_profile(self, profile_id: int | None) -> bool:
        """Whether the viewer and *profile_id* are hidden from each other.

        Args:
            profile_id: The other profile's pk.

        Returns:
            True while a block joins the two.
        """
        return profile_id in self.since

    def hides_content(self, author_id: int | None, created: datetime) -> bool:
        """Whether something *author_id* posted at *created* is hidden from the viewer, or the viewer's from them.

        Args:
            author_id: Who posted it - the other party, whichever direction the content travels.
            created: When it was posted.

        Returns:
            True when a block joins the two and was already in place at *created*.
        """
        started = self.since.get(author_id) if author_id is not None else None
        return started is not None and created >= started

    def hidden_from_at(self, created: datetime) -> frozenset[int]:
        """Who something the viewer posts at *created* is hidden from.

        Args:
            created: When it was posted.

        Returns:
            The pks of everyone :meth:`hides_content` would hide it from.
        """
        return frozenset(pk for pk, started in self.since.items() if created >= started)

    def hidden_content_q(self, *, author_field: str, created_field: str = "created") -> Q | None:
        """:meth:`hides_content` as a ``Q`` over rows naming their author.

        Args:
            author_field: The row's author column, e.g. ``"sender_id"``.
            created_field: The row's creation-time column.

        Returns:
            A ``Q`` matching the hidden rows, or None when nothing is hidden.
        """
        hidden: Q | None = None
        for author_id, started in self.since.items():
            clause = Q(**{author_field: author_id, f"{created_field}__gte": started})
            hidden = clause if hidden is None else hidden | clause
        return hidden

    def exclude_hidden[QS: QuerySet[Any]](self, queryset: QS, *, author_field: str, created_field: str = "created") -> QS:
        """Drop the rows :meth:`hides_content` would hide.

        Args:
            queryset: Rows naming their author and creation time.
            author_field: The row's author column.
            created_field: The row's creation-time column.

        Returns:
            *queryset* without the hidden rows.
        """
        hidden = self.hidden_content_q(author_field=author_field, created_field=created_field)
        return queryset if hidden is None else queryset.exclude(hidden)

    def exclude_hidden_profiles[QS: QuerySet[Any]](self, queryset: QS, *, profile_field: str) -> QS:
        """Drop the rows naming someone the viewer is hidden from - memberships in a roster, say.

        Args:
            queryset: Rows naming a profile.
            profile_field: The row's profile column.

        Returns:
            *queryset* without those rows.
        """
        return queryset.exclude(**{f"{profile_field}__in": self.hidden_profile_ids}) if self.since else queryset


def _blocked_rows(pair: Q) -> list[tuple[int, int, datetime]]:
    """The ``(from, to, blocked_at)`` of every current block matching *pair*.

    Args:
        pair: Which rows, by their two profile columns.

    Returns:
        One tuple per block.
    """
    from urbanlens.dashboard.models.friendship.model import Friendship

    rows = Friendship.objects.filter(pair, status=FriendshipStatus.BLOCKED).values_list("from_profile_id", "to_profile_id", "blocked_at")
    # The column is null only on rows that are not blocks; friendship_blocked_at_only_while_blocked holds that.
    return [(from_id, to_id, blocked_at) for from_id, to_id, blocked_at in rows if blocked_at is not None]
