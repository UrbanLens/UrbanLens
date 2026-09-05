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
* Ties keep the **older** row, matching what `FriendshipQuerySet.between` has
  been doing since the containment fix - so this migration and the code it
  replaces agree about which row was authoritative.

Every merge is logged with both ids and both statuses, because on a real
database this is the only record that the discarded row existed.
"""

from __future__ import annotations

import logging
from typing import Any

from django.db import migrations

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


def merge_reciprocal_rows(apps, schema_editor) -> None:
    """Collapse every `A->B` / `B->A` pair into the one row that survives."""
    friendship = apps.get_model("dashboard", "Friendship")

    # `Any` because a historical model has no static type - it is built from
    # the migration state at runtime. (mypy excludes `migrations/` for this
    # reason; the annotation is for the reader.)
    seen: dict[tuple[int, int], Any] = {}
    merged = 0
    for row in friendship.objects.order_by("created", "pk").iterator():
        key = (min(row.from_profile_id, row.to_profile_id), max(row.from_profile_id, row.to_profile_id))
        keeper = seen.get(key)
        if keeper is None:
            seen[key] = row
            continue

        # The loser's sides are relative to its own direction; map them onto the
        # keeper's before OR-ing, or a mute lands on the wrong person.
        reversed_row = row.from_profile_id != keeper.from_profile_id
        loser_from = row.muted_by_to_profile if reversed_row else row.muted_by_from_profile
        loser_to = row.muted_by_from_profile if reversed_row else row.muted_by_to_profile

        fields = []
        if loser_from and not keeper.muted_by_from_profile:
            keeper.muted_by_from_profile = True
            fields.append("muted_by_from_profile")
        if loser_to and not keeper.muted_by_to_profile:
            keeper.muted_by_to_profile = True
            fields.append("muted_by_to_profile")
        if _rank(row.status) < _rank(keeper.status):
            logger.warning(
                "Merging reciprocal friendships %s (%s) and %s (%s): keeping the more restrictive status",
                keeper.pk,
                keeper.status,
                row.pk,
                row.status,
            )
            keeper.status = row.status
            fields.append("status")
        if fields:
            keeper.save(update_fields=fields)

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
