"""Record the relevance rule each cached row was swept under, so the public-media sweep finds what it has not judged (P233)."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0049_building_location_names"),
    ]

    operations = [
        migrations.AddField(
            model_name="locationcache",
            name="relevance_rule",
            field=models.PositiveSmallIntegerField(default=0, db_default=0),
        ),
        migrations.AddIndex(
            model_name="locationcache",
            index=models.Index(fields=["source", "relevance_rule"], name="idxdb_loccache_relrule"),
        ),
    ]
