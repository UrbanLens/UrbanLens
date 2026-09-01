"""Equivalence-group resolution and admin mutations over the tag vocabulary.

Two distinct provider tags (OSM ``amenity=restaurant``, Overture
``building_subtype=restaurant``) can describe the same real-world concept.
This module decides, for one Place's actual tags, which ones are equivalent
and which single tag to show - and gives the admin page the mutations
(create/join/leave a group, change the preferred member) it needs.

Two ways two vocabulary entries end up equivalent:

- **Explicit**: an admin (or a confirmed "suggested" match) put them in the
  same :class:`ExternalTagGroup`. Always wins, even for a single-member
  ("singleton") group - the only way an admin can veto a coincidental
  default match is to give the colliding entries separate explicit groups.
- **Default**: neither has an explicit group, and they humanize to the same
  display text (see :func:`default_group_key`). Applies automatically, with
  no persisted record, to any two ungrouped entries regardless of source.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db import transaction
from django.db.models import Q

from urbanlens.dashboard.models.place.external_tag_group import ExternalTagGroup, ExternalTagVocabularyEntry
from urbanlens.dashboard.services.locations.external_tags import humanize_tag_value

if TYPE_CHECKING:
    from collections.abc import Sequence

    from urbanlens.dashboard.models.place.external_tag import PlaceExternalTag
    from urbanlens.dashboard.models.place.model import Place


class ExternalTagGroupError(Exception):
    """A mapping action was refused. ``safe_message`` is safe to show a caller verbatim."""

    def __init__(self, message: str) -> None:
        self.safe_message = message
        super().__init__(message)


def default_group_key(value: str) -> str:
    """The default-matching key for a tag value: its normalized display text.

    Two ungrouped entries with the same key are treated as equivalent with
    no admin action required - this is what makes two providers both
    reporting "Restaurant" collapse to one chip out of the box.

    Args:
        value: A raw tag value (not yet humanized).

    Returns:
        The case-folded, trimmed humanized value.
    """
    return humanize_tag_value(value).strip().lower()


def visible_tags_for_place(place: Place) -> list[PlaceExternalTag]:
    """One representative :class:`PlaceExternalTag` per equivalence group on ``place``.

    Only ever compares tags ``place`` actually has - dedup is between tags on
    the same place, so there's no need to know about a group's other members
    elsewhere. Within a group of 2+ tags this place carries, the vocabulary
    entry marked ``is_preferred`` wins if that specific tag is present here;
    otherwise the first tag in the existing ``-is_primary, source, key``
    order is kept.

    Args:
        place: The place whose tags to resolve.

    Returns:
        A subset of ``place.external_tags.all()``, in the same relative
        order, with equivalent tags collapsed to one each.
    """
    rows = list(place.external_tags.all())
    if not rows:
        return rows

    lookup_filter = Q()
    for row in rows:
        lookup_filter |= Q(source=row.source, key=row.key, value=row.value)
    vocab_by_tuple = {(v.source, v.key, v.value): v for v in ExternalTagVocabularyEntry.objects.filter(lookup_filter)}

    buckets: dict[str, list[PlaceExternalTag]] = {}
    bucket_order: list[str] = []
    for row in rows:
        entry = vocab_by_tuple.get((row.source, row.key, row.value))
        bucket_key = f"group:{entry.group_id}" if entry and entry.group_id else f"value:{default_group_key(row.value)}"
        if bucket_key not in buckets:
            buckets[bucket_key] = []
            bucket_order.append(bucket_key)
        buckets[bucket_key].append(row)

    visible: list[PlaceExternalTag] = []
    for bucket_key in bucket_order:
        members = buckets[bucket_key]
        if len(members) == 1:
            visible.append(members[0])
            continue
        preferred = next((row for row in members if (entry := vocab_by_tuple.get((row.source, row.key, row.value))) and entry.is_preferred), None)
        visible.append(preferred or members[0])
    return visible


@dataclass(frozen=True)
class SuggestedCluster:
    """A group of currently-ungrouped vocabulary entries that default-match.

    Attributes:
        key: The shared :func:`default_group_key` value.
        entries: The matching entries, in vocabulary ordering.
    """

    key: str
    entries: list[ExternalTagVocabularyEntry]


def suggested_clusters() -> list[SuggestedCluster]:
    """Currently-ungrouped entries clustered by default-matching key.

    Computed live, never persisted - an admin can "confirm" a cluster into a
    real :class:`ExternalTagGroup` via :func:`create_group`, or leave it: the
    default match keeps applying either way.

    Returns:
        Clusters with 2 or more members, ordered by key.
    """
    clusters: dict[str, list[ExternalTagVocabularyEntry]] = {}
    for entry in ExternalTagVocabularyEntry.objects.ungrouped():
        clusters.setdefault(default_group_key(entry.value), []).append(entry)
    return [SuggestedCluster(key=key, entries=entries) for key, entries in sorted(clusters.items()) if len(entries) >= 2]


@transaction.atomic
def create_group(entry_ids: Sequence[int], *, preferred_id: int | None = None) -> ExternalTagGroup:
    """Create a new group containing the given vocabulary entries.

    A single entry is allowed - it creates a singleton group, which is how an
    admin vetoes a coincidental default match (see the module docstring):
    the entry now has an explicit group, so :func:`visible_tags_for_place`
    stops applying default same-text matching to it.

    Args:
        entry_ids: Vocabulary entry ids to group together. Must have at
            least 1 entry, none of which already belong to a group.
        preferred_id: Which entry to mark preferred; defaults to the first
            of ``entry_ids``.

    Returns:
        The new group.

    Raises:
        ExternalTagGroupError: No entries given, an unknown id, or an entry
            that already belongs to a group.
    """
    if len(entry_ids) < 1:
        raise ExternalTagGroupError("Select at least one tag to group.")

    entries = list(ExternalTagVocabularyEntry.objects.filter(pk__in=entry_ids))
    if len(entries) != len(set(entry_ids)):
        raise ExternalTagGroupError("One or more tags could not be found.")
    if any(entry.group_id is not None for entry in entries):
        raise ExternalTagGroupError("One or more tags already belong to a group.")

    preferred = preferred_id if preferred_id is not None else entry_ids[0]
    group = ExternalTagGroup.objects.create()
    for entry in entries:
        entry.group = group
        entry.is_preferred = entry.pk == preferred
    ExternalTagVocabularyEntry.objects.bulk_update(entries, ["group", "is_preferred"])
    return group


@transaction.atomic
def move_entry(entry_id: int, target_group_id: int | None) -> int | None:
    """Move one entry to a different group, or ungroup it (``target_group_id=None``).

    Covers every drag-and-drop outcome on the admin page: dropped onto a
    group's list (join it), dragged straight from one group's list to
    another's (leave the first, join the second), or dropped back on the
    "Ungrouped" pool (leave whichever group it was in). Always joins as a
    non-preferred member - use :func:`set_preferred` to change that
    afterward.

    Args:
        entry_id: The vocabulary entry to move.
        target_group_id: The group to join, or ``None`` to ungroup.

    Returns:
        The id of the entry's *previous* group if this move emptied it (the
        caller should remove that group's now-stale card from the DOM),
        else ``None``. Also ``None`` for a no-op drop back where it started.

    Raises:
        ExternalTagGroupError: The entry, or a given target group, is unknown.
    """
    entry = ExternalTagVocabularyEntry.objects.filter(pk=entry_id).first()
    if entry is None:
        raise ExternalTagGroupError("Tag not found.")

    if entry.group_id == target_group_id:
        return None

    target_group = None
    if target_group_id is not None:
        target_group = ExternalTagGroup.objects.filter(pk=target_group_id).first()
        if target_group is None:
            raise ExternalTagGroupError("Group not found.")

    old_group = entry.group
    entry.group = target_group
    entry.is_preferred = False
    entry.save(update_fields=["group", "is_preferred", "updated"])

    if old_group is not None and not old_group.members.exists():
        emptied_id = old_group.pk
        old_group.delete()
        return emptied_id
    return None


def set_preferred(entry_id: int, group_id: int) -> None:
    """Mark one entry as the member shown for its group.

    Args:
        entry_id: The vocabulary entry to prefer.
        group_id: The group it must belong to.

    Raises:
        ExternalTagGroupError: The entry doesn't belong to ``group_id``.
    """
    entry = ExternalTagVocabularyEntry.objects.filter(pk=entry_id, group_id=group_id).first()
    if entry is None:
        raise ExternalTagGroupError("Tag not found in that group.")
    with transaction.atomic():
        ExternalTagVocabularyEntry.objects.filter(group_id=group_id, is_preferred=True).update(is_preferred=False)
        entry.is_preferred = True
        entry.save(update_fields=["is_preferred", "updated"])
