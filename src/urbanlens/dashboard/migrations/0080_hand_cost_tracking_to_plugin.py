"""
Hand the cost tracking models to a plugin's app, which adopts their tables.

State only: the tables stay where they are until that app's first migration renames them, and are left alone where it
is not installed.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0079_profile_member_badge"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.DeleteModel(
                    name="CostComponent",
                ),
                migrations.DeleteModel(
                    name="OperatingCost",
                ),
            ],
        ),
    ]
