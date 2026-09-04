"""Record what each wiki edit paid, and which edits are reverts.

Consensus paid a flat 3 points for every ``WikiEdit`` with an editor and no
``consensus_round``, and never took any of it back. A revert is itself a
``WikiEdit``, so reverting somebody's work earned points and an edit war paid
both sides on every pass; an alias earned the same as a rewritten description;
and a contribution later reverted kept its award.

Three columns, and what each is for:

``is_revert``
    Decided at creation rather than derived from the ``reverted_by``
    back-reference, because the award happens in the reverting row's own
    ``post_save`` - before the target row has been updated to point at it.

``consensus_points``
    What this row actually paid. Stored rather than recomputed because the
    weights in ``services.consensus.points`` are a first cut expected to be
    retuned, and a retraction has to return exactly what was paid.

``consensus_points_retracted``
    The compare-and-swap flag that makes retraction idempotent and reversible -
    several paths reach it for one row (the revert, an admin toggling
    ``reverted``, deleting an already-reverted edit), and reverting a revert has
    to put the award back.

The backfill records ``MANUAL_EDIT_POINTS`` on existing rows because that is
what they were paid, not what the new weights would give them. Rows that are
already reverted keep their award: declining to pay new points for a revert is
one thing, draining totals people have already been shown is another, and this
migration deliberately does not do the second.

The data step's body lives in ``services.consensus.points`` so it can be
exercised by a test - this repo has no migration-test harness, so logic left
inline here is logic nothing runs until deploy.
"""

from django.db import migrations, models


def _backfill(apps, schema_editor):
    """Record the historical award and flag existing reverts.

    Args:
        apps: The historical app registry.
        schema_editor: Unused; required by ``RunPython``.
    """
    from urbanlens.dashboard.services.consensus.points import backfill_wiki_edit_points

    backfill_wiki_edit_points(apps.get_model("dashboard", "WikiEdit"))


class Migration(migrations.Migration):
    """Add the three award-accounting columns to ``WikiEdit`` and backfill them."""

    dependencies = [
        ("dashboard", "0051_image_profile_kind_recent_index"),
    ]

    operations = [
        migrations.AddField(
            model_name="wikiedit",
            name="is_revert",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="wikiedit",
            name="consensus_points",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="wikiedit",
            name="consensus_points_retracted",
            field=models.BooleanField(default=False),
        ),
        # Last, after every schema change on this table: a RunPython poisons the
        # table for the rest of the transaction.
        migrations.RunPython(_backfill, migrations.RunPython.noop),
    ]
