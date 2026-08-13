"""Merge duplicate labels so the constraint in 0043 can be added.

Split from the ``AddConstraint`` deliberately. Django runs one migration in one
transaction, and Postgres refuses to build an index in a transaction that has
already modified the table's rows - the foreign keys here are DEFERRABLE
INITIALLY DEFERRED, so the merge leaves pending trigger events and
``AddConstraint`` fails with "cannot CREATE INDEX ... because it has pending
trigger events". Two migrations means two transactions: the merge commits, then
the index is built against settled data.

``Label`` had no uniqueness at all - its only unique indexes were ``id`` and
``uuid`` - while nine call sites used ``get_or_create`` on
``(profile, name, kind)`` as though it identified a row. Two concurrent writes
therefore both missed and both inserted, and a later ``.get(name=...)`` raised
``MultipleObjectsReturned``.

The constraint cannot simply be added: any existing violation makes the
``AddConstraint`` fail, so the data has to be merged first. Two passes, in this
order:

1. **Personal labels shadowing a global one** are merged into the global label,
   which survives. A user's private label and a global label with the same name
   do not violate the constraint (their ``profile`` differs), so this pass is
   broader than the constraint requires - it is here because a personal label
   duplicating a global one is the same confusion for the user, and the global
   is the one every other profile already sees.
2. **Duplicates within one owner** are merged into the oldest row, which keeps
   the id that existing rows already point at.

"Merging" moves exactly what ``services.labels.merge`` moves - pins, wikis,
images, profile assignments and child links - reimplemented here against the
through tables directly, because a migration must not import application code
that may have moved on by the time it runs.
"""

from __future__ import annotations

from django.db import migrations


def _merge(cursor, *, keep_id: int, drop_ids: list[int]) -> None:
    """Repoint everything attached to *drop_ids* onto *keep_id*, then delete them.

    Each statement is an idempotent "move what is not already there, delete the
    rest" pair rather than a bare UPDATE: a pin carrying both the surviving and a
    duplicated label would otherwise violate the through table's own
    (pin, label) uniqueness the moment the second row was repointed.
    """
    if not drop_ids:
        return

    through = (
        ("dashboard_user_pins_labels", "pin"),
        ("dashboard_wikis_labels", "wiki"),
        ("dashboard_images_labels", "image"),
    )
    for table, owner in through:
        cursor.execute(
            f"UPDATE {table} SET label_id = %s WHERE label_id = ANY(%s) "  # noqa: S608
            f"AND {owner}_id NOT IN (SELECT {owner}_id FROM {table} WHERE label_id = %s)",
            [keep_id, drop_ids, keep_id],
        )
        cursor.execute(f"DELETE FROM {table} WHERE label_id = ANY(%s)", [drop_ids])  # noqa: S608

    # Profile assignments: same shape, keyed on (author, subject).
    cursor.execute(
        "UPDATE dashboard_profile_label_assignments SET label_id = %s WHERE label_id = ANY(%s) "
        "AND (author_id, subject_id) NOT IN "
        "(SELECT author_id, subject_id FROM dashboard_profile_label_assignments WHERE label_id = %s)",
        [keep_id, drop_ids, keep_id],
    )
    cursor.execute("DELETE FROM dashboard_profile_label_assignments WHERE label_id = ANY(%s)", [drop_ids])

    # Hierarchy: reparent children onto the survivor, and move the dropped rows'
    # own parents across, dropping any edge that would make the survivor its own
    # parent.
    cursor.execute(
        "UPDATE dashboard_labels_parents SET to_label_id = %s WHERE to_label_id = ANY(%s) "
        "AND from_label_id <> %s "
        "AND from_label_id NOT IN (SELECT from_label_id FROM dashboard_labels_parents WHERE to_label_id = %s)",
        [keep_id, drop_ids, keep_id, keep_id],
    )
    cursor.execute(
        "UPDATE dashboard_labels_parents SET from_label_id = %s WHERE from_label_id = ANY(%s) "
        "AND to_label_id <> %s "
        "AND to_label_id NOT IN (SELECT to_label_id FROM dashboard_labels_parents WHERE from_label_id = %s)",
        [keep_id, drop_ids, keep_id, keep_id],
    )
    cursor.execute(
        "DELETE FROM dashboard_labels_parents WHERE from_label_id = ANY(%s) OR to_label_id = ANY(%s)",
        [drop_ids, drop_ids],
    )

    # Customizations are per (profile, label); keep one per profile.
    cursor.execute(
        "UPDATE dashboard_label_customizations SET label_id = %s WHERE label_id = ANY(%s) "
        "AND profile_id NOT IN (SELECT profile_id FROM dashboard_label_customizations WHERE label_id = %s)",
        [keep_id, drop_ids, keep_id],
    )
    cursor.execute("DELETE FROM dashboard_label_customizations WHERE label_id = ANY(%s)", [drop_ids])

    cursor.execute("DELETE FROM dashboard_labels WHERE id = ANY(%s)", [drop_ids])


def merge_duplicate_labels(apps, schema_editor) -> None:
    """Collapse every group that would violate the new constraint."""
    connection = schema_editor.connection
    with connection.cursor() as cursor:
        # Pass 1: personal labels shadowing a global one. The global survives.
        cursor.execute(
            """
            SELECT g.id, array_agg(p.id)
            FROM dashboard_labels g
            JOIN dashboard_labels p
              ON lower(p.name) = lower(g.name) AND p.kind = g.kind AND p.profile_id IS NOT NULL
            WHERE g.profile_id IS NULL
            GROUP BY g.id
            """
        )
        for keep_id, drop_ids in cursor.fetchall():
            _merge(cursor, keep_id=keep_id, drop_ids=list(drop_ids))

        # Pass 2: duplicates within one owner (including global-vs-global, which
        # nulls_distinct=False makes a violation). Oldest row survives.
        cursor.execute(
            """
            SELECT array_agg(id ORDER BY created, id)
            FROM dashboard_labels
            GROUP BY lower(name), profile_id, kind
            HAVING count(*) > 1
            """
        )
        for (ids,) in cursor.fetchall():
            _merge(cursor, keep_id=ids[0], drop_ids=list(ids[1:]))


def noop_reverse(apps, schema_editor) -> None:
    """Merging cannot be undone - the dropped rows are gone.

    Reversing the migration removes the constraint (the operation below), which
    is enough to get the schema back; the merged data stays merged.
    """


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0041_pin_import_failure_maps_url"),
    ]

    operations = [
        migrations.RunPython(merge_duplicate_labels, noop_reverse),
    ]
