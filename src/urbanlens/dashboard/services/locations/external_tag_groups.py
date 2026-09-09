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

from django.core.cache import cache
from django.db import transaction
from django.db.models import Q

from urbanlens.dashboard.models.place.external_tag_group import ExternalTagGroup, ExternalTagVocabularyEntry
from urbanlens.dashboard.services.locations.external_tags import humanize_tag_value

if TYPE_CHECKING:
    from collections.abc import Sequence

    from urbanlens.dashboard.models.place.external_tag import PlaceExternalTag
    from urbanlens.dashboard.models.place.model import Place

# The whole vocabulary table, materialized. Small and admin-curated (see the
# module docstring) but reloaded on every search keystroke through
# matching_vocabulary()/tag_match_q() - once per term, per provider - so it is
# worth caching wholesale rather than per-query. Invalidated explicitly by
# _invalidate_vocabulary_cache() rather than left to the default TTL: a write
# must be visible on the very next call, not up to five minutes later.
_VOCABULARY_CACHE_KEY = "external_tag_vocabulary:all"


def _cached_vocabulary_entries() -> list[ExternalTagVocabularyEntry]:
    """The full vocabulary table, served from cache and refilled lazily on a miss.

    Returns:
        Every :class:`ExternalTagVocabularyEntry`, in no particular order.
    """
    entries = cache.get(_VOCABULARY_CACHE_KEY)
    if entries is None:
        entries = list(ExternalTagVocabularyEntry.objects.all())
        cache.set(_VOCABULARY_CACHE_KEY, entries)
    return entries


def _invalidate_vocabulary_cache() -> None:
    """Drop the cached vocabulary table so the next read reloads it.

    Call this from anything that changes an entry's group membership or
    preference, or that changes which groups exist - not just the three
    obvious admin actions below: :meth:`PlaceExternalTag.sync_for_source`
    also writes new rows here (see ``ExternalTagVocabularyEntry``'s
    docstring) and invalidates through this same helper.
    """
    cache.delete(_VOCABULARY_CACHE_KEY)


class ExternalTagGroupError(Exception):
    """A mapping action was refused.

    The message is for logs, not the response: an HTTP-facing caller should
    catch a specific subclass below (or this base class as a fallback) and
    author its own user-facing text, rather than relaying the message - that
    keeps a future raise site here from being able to smuggle unreviewed text
    into a response just by adding a new ``raise``.
    """


class EmptySelectionError(ExternalTagGroupError):
    """No vocabulary entry ids were given to group."""


class UnknownVocabularyEntryError(ExternalTagGroupError):
    """One or more given vocabulary entry ids don't exist."""


class AlreadyGroupedError(ExternalTagGroupError):
    """One or more given entries already belong to a group."""


class UnknownGroupError(ExternalTagGroupError):
    """The given target group id doesn't exist."""


class EntryNotInGroupError(ExternalTagGroupError):
    """The given entry doesn't belong to the given group."""


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


def _bucket_key(entry: ExternalTagVocabularyEntry) -> str:
    """The equivalence-bucket key for one vocabulary entry.

    Shared by :func:`visible_tags_for_place` (resolving what to *show*) and
    :func:`matching_vocabulary` (resolving what a search term *reaches*) -
    both are the same "which tags count as the same thing" question, just
    run in opposite directions.
    """
    return f"group:{entry.group_id}" if entry.group_id else f"value:{default_group_key(entry.value)}"


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
        bucket_key = _bucket_key(entry) if entry else f"value:{default_group_key(row.value)}"
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


def _loosely_contains(haystack: str, needle: str) -> bool:
    """Substring match tolerant of a simple trailing-``s`` plural mismatch.

    Exists so searching "restaurants" finds a tag whose humanized value is
    "Restaurant" - plain ``in`` fails there since the longer word is never a
    substring of the shorter one. Deliberately not a general stemmer: one
    trailing-``s`` strip, used only for tag search matching.

    Args:
        haystack: Lowercased text to search within.
        needle: Lowercased search term.

    Returns:
        Whether ``needle`` (or its de-pluralized form) appears in ``haystack``.
    """
    if not needle:
        return False
    if needle in haystack:
        return True
    singular = needle.rstrip("s")
    return bool(singular) and singular != needle and singular in haystack


def matching_vocabulary(term: str) -> list[ExternalTagVocabularyEntry]:
    """Every vocabulary entry equivalent to any entry whose display text matches ``term``.

    "Equivalent" is the same bucketing :func:`visible_tags_for_place` uses -
    an explicit group, or a shared :func:`default_group_key` - run in the
    opposite direction: starting from a search term rather than a place's
    tags. This is what makes searching one provider's wording (e.g. OSM's
    "amenity=restaurant") also reach an admin-grouped equivalent from another
    provider (Overture's "building_subtype=restaurant"), the same way the
    wiki chip already collapses them to one.

    Args:
        term: A single search token (already lowercased or not - normalized
            here).

    Returns:
        Matching entries across every matched equivalence bucket. Empty for
        a blank term or no match.
    """
    normalized = term.strip().lower()
    if not normalized:
        return []
    entries = _cached_vocabulary_entries()
    matched_buckets = {_bucket_key(entry) for entry in entries if _loosely_contains(humanize_tag_value(entry.value).lower(), normalized)}
    if not matched_buckets:
        return []
    return [entry for entry in entries if _bucket_key(entry) in matched_buckets]


def tag_match_q(term: str, path: str) -> Q:
    """A Q object matching :class:`PlaceExternalTag` rows equivalent to ``term``.

    Args:
        term: A single search token.
        path: ORM path to a ``PlaceExternalTag`` relation (e.g.
            ``"location__place__external_tags"``).

    Returns:
        The OR of every matched ``(source, key, value)`` triple, anchored at
        ``path``. When nothing matches, returns ``Q(pk__in=())`` - a
        guaranteed-false Q - rather than an empty ``Q()``. An empty ``Q()``
        is a no-op filter (matches everything); OR-ing that into a per-term
        AND-across-terms clause would silently turn "no tag match" into
        "match every row" once callers stop guarding for it.
    """
    entries = matching_vocabulary(term)
    if not entries:
        return Q(pk__in=())
    combined = Q()
    for entry in entries:
        combined |= Q(**{f"{path}__source": entry.source, f"{path}__key": entry.key, f"{path}__value": entry.value})
    return combined


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
        EmptySelectionError: ``entry_ids`` was empty.
        UnknownVocabularyEntryError: One or more ids don't resolve to an
            existing entry.
        AlreadyGroupedError: One or more entries already belong to a group.
    """
    if len(entry_ids) < 1:
        raise EmptySelectionError("create_group called with an empty entry_ids.")

    entries = list(ExternalTagVocabularyEntry.objects.filter(pk__in=entry_ids))
    if len(entries) != len(set(entry_ids)):
        missing = set(entry_ids) - {entry.pk for entry in entries}
        raise UnknownVocabularyEntryError(f"create_group: entry_ids {sorted(missing)} do not exist.")
    if already_grouped := [entry.pk for entry in entries if entry.group_id is not None]:
        raise AlreadyGroupedError(f"create_group: entry_ids {already_grouped} already belong to a group.")

    preferred = preferred_id if preferred_id is not None else entry_ids[0]
    group = ExternalTagGroup.objects.create()
    for entry in entries:
        entry.group = group
        entry.is_preferred = entry.pk == preferred
    ExternalTagVocabularyEntry.objects.bulk_update(entries, ["group", "is_preferred"])
    _invalidate_vocabulary_cache()
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
        UnknownVocabularyEntryError: ``entry_id`` doesn't exist.
        UnknownGroupError: ``target_group_id`` was given but doesn't exist.
    """
    entry = ExternalTagVocabularyEntry.objects.filter(pk=entry_id).first()
    if entry is None:
        raise UnknownVocabularyEntryError(f"move_entry: entry_id {entry_id} does not exist.")

    if entry.group_id == target_group_id:
        return None

    target_group = None
    if target_group_id is not None:
        target_group = ExternalTagGroup.objects.filter(pk=target_group_id).first()
        if target_group is None:
            raise UnknownGroupError(f"move_entry: target_group_id {target_group_id} does not exist.")

    old_group = entry.group
    entry.group = target_group
    entry.is_preferred = False
    entry.save(update_fields=["group", "is_preferred", "updated"])
    # Covers the old-group-delete branch below too: that deletion never
    # changes any surviving entry's bucket by itself (the group was already
    # empty), so one invalidation here - for the group_id change just saved -
    # is enough for both outcomes.
    _invalidate_vocabulary_cache()

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
        EntryNotInGroupError: The entry doesn't belong to ``group_id``.
    """
    entry = ExternalTagVocabularyEntry.objects.filter(pk=entry_id, group_id=group_id).first()
    if entry is None:
        raise EntryNotInGroupError(f"set_preferred: entry_id {entry_id} does not belong to group_id {group_id}.")
    with transaction.atomic():
        ExternalTagVocabularyEntry.objects.filter(group_id=group_id, is_preferred=True).update(is_preferred=False)
        entry.is_preferred = True
        entry.save(update_fields=["is_preferred", "updated"])
    _invalidate_vocabulary_cache()
