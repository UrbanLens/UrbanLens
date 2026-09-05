"""Merge reciprocal `Friendship` rows so one row per pair can be enforced.

`unique_together = ("from_profile", "to_profile")` stops a duplicate in one
direction and permits `A->B` *and* `B->A` to both exist, while every reader
assumes they cannot - the model docstring says "exactly one row per pair", and
the mute columns are per-side of one row. A profile import restoring both
directions, or two simultaneous requests in opposite directions, produces the
pair.

This merges any that exist. The constraint that stops them coming back is a
separate migration on purpose: adding it in the same one risks Postgres
reporting pending trigger events from the `RunPython` above it.

**The merge rule, and why it is this one.** The two rows can hold different
`status` values and nothing records which is right, so the rule is chosen to be
safe under every combination rather than to guess at history:

* The **more restrictive** status wins. `Blocked` beats everything, then
  `Removed`, `Declined`, `Ignored` - each an explicit "no" that a merge must not
  quietly undo. `Accepted` beats `Requested`/`Pending`, because a stale request
  alongside an accepted friendship is the redundant half, and choosing the
  request would revoke a real friendship.
* **Every mute survives.** Mute is a preference, and the columns are per-side of
  a row, so the loser's flags are mapped onto the winner's sides (swapped, since
  the row is reversed) and OR-ed in. Losing a mute means sending someone
  notifications they switched off.
* **The winning status brings its direction with it.** `Friendship` has no
  "blocked_by" column - `from_profile` *is* the blocker, and for a request it is
  the asker - so adopting the loser's status without swapping the keeper's ends
  would record the blocked party as the blocker. This codebase has already had
  that defect once, from a different cause, and carries a read-only audit
  command for it (`audit_inverted_friendship_blocks`). The mute columns and
  `request_message` travel with the ends.
* Ties keep the **lowest-pk** row - the one `FriendshipQuerySet.between` has been
  answering with since the containment fix, so the merge preserves the identity the
  application has already been using. (`created` would have been the intuitive key and is the
  wrong one: it is restorable from an import, so the two orders can disagree.)

Every merge is logged with both ids and both statuses, because on a real
database this is the only record that the discarded row existed.
"""

from __future__ import annotations

import logging
from typing import Any

from django.db import migrations
from django.db.models import Count
from django.db.models.functions import Greatest, Least

logger = logging.getLogger(__name__)

#: Most restrictive first. A status absent here sorts last, so an unrecognised
#: value never silently outranks a real refusal.
_STATUS_PRECEDENCE = ("Blocked", "Removed", "Declined", "Ignored", "Accepted", "Requested", "Pending", "Muted")


def _rank(status: str) -> int:
    """How restrictive `status` is; lower wins.

    Args:
        status: A stored ``FriendshipStatus`` value.

    Returns:
        Its index in the precedence order, or one past the end when unknown.
    """
    try:
        return _STATUS_PRECEDENCE.index(status)
    except ValueError:
        return len(_STATUS_PRECEDENCE)


def _duplicated_pairs(friendship: Any) -> list[tuple[int, int]]:
    """The `(lower id, higher id)` pairs that have more than one row.

    Asked of the database rather than derived while walking every row: the
    accumulate-as-you-go version holds one model instance per *distinct pair*
    for the length of the table, so `.iterator()` bounds nothing and a large
    friendships table is loaded into the migration's memory to find what is
    usually a handful of duplicates.

    Args:
        friendship: The historical ``Friendship`` model.

    Returns:
        One tuple per duplicated pair.
    """
    duplicated = (
        friendship.objects.annotate(low=Least("from_profile_id", "to_profile_id"), high=Greatest("from_profile_id", "to_profile_id"))
        .values("low", "high")
        .annotate(rows=Count("pk"))
        .filter(rows__gt=1)
    )
    return [(entry["low"], entry["high"]) for entry in duplicated]


def merge_reciprocal_rows(apps, schema_editor) -> None:
    """Collapse every `A->B` / `B->A` pair into the one row that survives."""
    friendship = apps.get_model("dashboard", "Friendship")

    pairs = _duplicated_pairs(friendship)
    if not pairs:
        return

    # `Any` because a historical model has no static type - it is built from
    # the migration state at runtime. (mypy excludes `migrations/` for this
    # reason; the annotation is for the reader.)
    seen: dict[tuple[int, int], Any] = {}
    merged = 0
    pair_set = set(pairs)
    involved = {profile for pair in pairs for profile in pair}
    candidates = friendship.objects.filter(from_profile_id__in=involved, to_profile_id__in=involved)
    # By `pk`, matching `FriendshipQuerySet.between`, which has been answering
    # with the lowest-pk row since the containment fix. Ordering by `created`
    # instead would let the migration keep a row the application has *not*
    # been treating as authoritative - `created` is restorable from an import,
    # so the two orders can disagree.
    for row in candidates.order_by("pk").iterator():
        key = (min(row.from_profile_id, row.to_profile_id), max(row.from_profile_id, row.to_profile_id))
        # `involved` can pull in a row joining two profiles that each appear in
        # *different* duplicated pairs but are not a duplicated pair themselves.
        if key not in pair_set:
            continue
        keeper = seen.get(key)
        if keeper is None:
            seen[key] = row
            continue

        reversed_row = row.from_profile_id != keeper.from_profile_id
        fields = []

        # The status decides the *direction* too, and getting that wrong is the
        # one way this migration could corrupt rather than merge. `Friendship`
        # has no "blocked_by" column: `from_profile` is the blocker, and for a
        # request it is the asker. So adopting the loser's status without its
        # orientation would record the blocked party as the blocker - which is
        # a real, already-seen defect in this codebase (see
        # `management/commands/audit_inverted_friendship_blocks.py`, written for
        # exactly that shape).
        if _rank(row.status) < _rank(keeper.status):
            logger.warning(
                "Merging reciprocal friendships %s (%s) and %s (%s): keeping the more restrictive status",
                keeper.pk,
                keeper.status,
                row.pk,
                row.status,
            )
            if reversed_row:
                keeper.from_profile_id, keeper.to_profile_id = keeper.to_profile_id, keeper.from_profile_id
                # Positional, so they travel with the ends they describe.
                keeper.muted_by_from_profile, keeper.muted_by_to_profile = (
                    keeper.muted_by_to_profile,
                    keeper.muted_by_from_profile,
                )
                fields += ["from_profile", "to_profile", "muted_by_from_profile", "muted_by_to_profile"]
                reversed_row = False
            keeper.status = row.status
            # The message belongs to whoever asked, which is now this row's asker.
            keeper.request_message = row.request_message
            fields += ["status", "request_message"]

        # The loser's mute flags are relative to *its* direction; map them onto
        # the keeper's (which may have just been flipped) before OR-ing, or a
        # mute lands on the wrong person.
        loser_from = row.muted_by_to_profile if reversed_row else row.muted_by_from_profile
        loser_to = row.muted_by_from_profile if reversed_row else row.muted_by_to_profile
        if loser_from and not keeper.muted_by_from_profile:
            keeper.muted_by_from_profile = True
            fields.append("muted_by_from_profile")
        if loser_to and not keeper.muted_by_to_profile:
            keeper.muted_by_to_profile = True
            fields.append("muted_by_to_profile")

        if fields:
            keeper.save(update_fields=sorted(set(fields)))

        logger.warning("Deleting reciprocal friendship row %s (%s -> %s), merged into %s", row.pk, row.from_profile_id, row.to_profile_id, keeper.pk)
        row.delete()
        merged += 1

    if merged:
        logger.warning("Merged %s reciprocal friendship row(s)", merged)


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0053_image_upload_failure_state")]

    operations = [
        # Irreversible by nature: the discarded row's own status and mute flags
        # are gone, and which of two conflicting statuses was "right" was never
        # recorded. The log lines above are the only trace, which is why they
        # are warnings rather than debug.
        migrations.RunPython(merge_reciprocal_rows, migrations.RunPython.noop),
    ]
