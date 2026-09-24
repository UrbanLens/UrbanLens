import zoneinfo

import django.db.models.functions.datetime
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0073_dedupe_rows_before_race_constraints")]

    operations = [
        migrations.AddConstraint(
            model_name="devicescanupload",
            constraint=models.UniqueConstraint(condition=models.Q(("client_session_uuid", ""), _negated=True), fields=("client_session_uuid",), name="db_scanupload_one_per_client_session"),
        ),
        migrations.AddConstraint(
            model_name="markupmapshare",
            constraint=models.UniqueConstraint(fields=("markup_map", "from_profile", "to_profile"), name="db_mapshare_one_per_map_pair"),
        ),
        migrations.AddConstraint(
            model_name="pinsuggestion",
            constraint=models.UniqueConstraint(condition=models.Q(("origin", "community")), fields=("profile", "location"), name="db_pin_sugg_one_community_per_loc"),
        ),
        migrations.AddConstraint(
            model_name="pinvisit",
            constraint=models.UniqueConstraint(
                models.F("pin"),
                django.db.models.functions.datetime.TruncDate("visited_at", tzinfo=zoneinfo.ZoneInfo(key="UTC")),
                condition=models.Q(("source", "geolocation")),
                name="db_pv_one_geo_per_pin_day",
            ),
        ),
    ]
