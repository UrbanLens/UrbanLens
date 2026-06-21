"""Sync BadgeCustomization field state to match the current model code.

After 0044's state-only RenameField("tag" → "badge"), Django's migration state
is missing three details that the model code already specifies:

1. badge FK: db_column="tag_id" (the physical column was always tag_id; Django's
   state defaulted to badge_id after the rename and would try to rename the
   column without this fix).
2. profile FK: related_name="badge_customizations" (was tag_customizations in 0041).
3. id pk: BigAutoField, matching DEFAULT_AUTO_FIELD set in the project (0041 used
   AutoField explicitly).

All three are state-only - the DB already matches. Instances that ran the
original 0044 need this migration to bring their state in sync; fresh instances
will run it immediately after 0044 and reach the same final state.
"""

from __future__ import annotations

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0044_rename_tag_to_badge"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                # Adds db_column="tag_id" so Django knows the physical column
                # name, preventing a spurious RENAME COLUMN migration.
                migrations.AlterField(
                    model_name="badgecustomization",
                    name="badge",
                    field=models.ForeignKey(
                        db_column="tag_id",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="customizations",
                        to="dashboard.badge",
                    ),
                ),
                # related_name changed from "tag_customizations" to
                # "badge_customizations" in the model; no DB column involved.
                migrations.AlterField(
                    model_name="badgecustomization",
                    name="profile",
                    field=models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="badge_customizations",
                        to="dashboard.profile",
                    ),
                ),
                # Sync pk type to BigAutoField (DEFAULT_AUTO_FIELD).
                # Migration 0041 used AutoField explicitly; the DB integer column
                # is unaffected - this is a state-only correction.
                migrations.AlterField(
                    model_name="badgecustomization",
                    name="id",
                    field=models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
            ],
            database_operations=[],
        ),
    ]
