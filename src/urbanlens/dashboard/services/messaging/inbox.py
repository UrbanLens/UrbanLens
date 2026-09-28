"""The unified inbox - one-to-one threads and group chats - ordered and paged in SQL."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.db.models import Max, Value

from urbanlens.dashboard.models.direct_messages.model import DirectMessage
from urbanlens.dashboard.services.messaging.direct_messages import build_dm_conversations
from urbanlens.dashboard.services.messaging.group_chats import GROUP_INBOX_FIELDS, build_group_conversations, group_inbox_rows

if TYPE_CHECKING:
    from collections.abc import Iterator

    from django.db.models import QuerySet

    from urbanlens.dashboard.models.profile.model import Profile

_DM = "dm"
_GROUP = "group"
_ITER_PAGE = 100


class InboxFeed:
    """A profile's conversations, newest activity first, supporting ``len()`` and slicing.

    One ``UNION`` of the per-partner direct-message aggregate and the annotated group memberships is
    ordered and cut by the database; only the rows in the requested slice are then built into
    conversation dicts. ``Paginator`` and DRF's paginator ask only for ``len()`` and one slice.

    Not a ``Sequence``: a conversation whose data vanishes between the index query and the build is
    dropped, so a slice can come back shorter than asked and a position has no stable answer.

    Args:
        profile: Whose inbox.
        only_unread: Keep only conversations with an unread message.
    """

    def __init__(self, profile: Profile, *, only_unread: bool = False) -> None:
        self.profile = profile
        self.only_unread = only_unread

    def _index(self) -> QuerySet[Any, tuple[Any, ...]]:
        """``(kind, key, last_id, last_activity, unread)`` for every conversation, ordered newest first."""
        dm = DirectMessage.objects.visible_to(self.profile).conversation_rows(self.profile).annotate(last_activity=Max("created")).order_by()
        if self.only_unread:
            dm = dm.filter(unread_count__gt=0)
        dm_index = dm.annotate(kind=Value(_DM)).values_list("kind", "partner_id", "last_message_id", "last_activity", "unread_count")
        group_index = group_inbox_rows(self.profile, only_unread=self.only_unread).order_by().annotate(kind=Value(_GROUP)).values_list("kind", "group_id", "last_id", "last_activity", "unread")
        return dm_index.union(group_index, all=True).order_by("-last_activity", "kind", "-partner_id")

    def __len__(self) -> int:
        """How many conversations the inbox holds."""
        return self._index().count()

    def __getitem__(self, index: slice) -> list[dict[str, Any]]:
        """One slice of the conversations, cut in SQL; bounds counted from the end need the whole inbox.

        Args:
            index: The slice to take.

        Returns:
            The built conversations, possibly fewer than the slice spans.

        Raises:
            TypeError: *index* is not a slice.
        """
        if not isinstance(index, slice):
            raise TypeError("InboxFeed supports slices only.")
        counts_from_the_end = any(bound is not None and bound < 0 for bound in (index.start, index.stop)) or (index.step is not None and index.step < 0)
        if counts_from_the_end:
            return self._build(list(self._index()))[index]
        built = self._build(list(self._index()[index.start : index.stop]))
        return built[:: index.step] if index.step else built

    def __iter__(self) -> Iterator[dict[str, Any]]:
        """Every conversation, fetched a page at a time."""
        offset = 0
        while True:
            rows = list(self._index()[offset : offset + _ITER_PAGE])
            yield from self._build(rows)
            if len(rows) < _ITER_PAGE:
                return
            offset += _ITER_PAGE

    def _build(self, rows: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
        """Conversation dicts for index *rows*, in their order; a row whose data vanished meanwhile is dropped."""
        dm_rows = [{"partner_id": key, "last_message_id": last_id, "unread_count": unread} for kind, key, last_id, _activity, unread in rows if kind == _DM]
        group_ids = [key for kind, key, *_rest in rows if kind == _GROUP]
        dms = {conv["partner"].pk: conv for conv in build_dm_conversations(self.profile, dm_rows)}
        groups = {}
        if group_ids:
            group_rows = list(group_inbox_rows(self.profile).filter(group_id__in=group_ids).values(*GROUP_INBOX_FIELDS))
            groups = {conv["group"].pk: conv for conv in build_group_conversations(self.profile, group_rows)}
        built = []
        for kind, key, *_rest in rows:
            conversation = (dms if kind == _DM else groups).get(key)
            if conversation is not None:
                built.append(conversation)
        return built
