"""
Add the lineage and earned-family grant reasons.

Existing ``split`` rows are left alone: a split snapshot and an earned-family snapshot wrote the
same rows (the aggregate and its MEMBER_OF children) under the same reason, and nothing recorded which
writer ran, so no row can be reliably reassigned.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0003_profiletrustsignals")]

    operations = [
        migrations.AlterField(
            model_name="placeaccessgrant",
            name="reason",
            field=models.CharField(
                choices=[
                    ("backfill", "Held before places existed"),
                    ("split", "Held the parcel when it was subdivided"),
                    ("lineage", "Held a place when it was merged or reclassified"),
                    ("earned_family", "Pinned every current member of a split family"),
                    ("engagement", "Viewed the wiki or shared content to it while access was held"),
                ],
                max_length=20,
            ),
        ),
    ]
