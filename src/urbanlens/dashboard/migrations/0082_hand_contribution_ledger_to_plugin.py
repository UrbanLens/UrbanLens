"""
Hand the contribution ledger's models to a plugin's app, which adopts their tables.

State only: the tables stay where they are until that app's first migration renames them, and are left alone where it
is not installed.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0081_drop_dollar_cost"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.DeleteModel(
                    name="ProfileReputation",
                ),
                migrations.DeleteModel(
                    name="ReputationEvent",
                ),
            ],
        ),
    ]
