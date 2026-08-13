"""Add the uniqueness constraint, after 0042 has merged the duplicates.

Separate from the merge because Django runs a migration in a single transaction
and Postgres will not build an index in a transaction that already modified the
table (the FKs are DEFERRABLE INITIALLY DEFERRED, so the merge leaves pending
trigger events). Splitting gives the merge its own committed transaction.

This also satisfies the project's ordering rule directly: index creation goes
dead last in the chain.
"""

from __future__ import annotations

from django.db import migrations, models
import django.db.models.functions.text


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0042_label_merge_duplicates"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="label",
            constraint=models.UniqueConstraint(
                django.db.models.functions.text.Lower("name"),
                models.F("profile"),
                models.F("kind"),
                name="uq_label_profile_name_kind_ci",
                nulls_distinct=False,
            ),
        ),
    ]
