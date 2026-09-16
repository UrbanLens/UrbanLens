from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0047_profile_map_center_stale_since"),
    ]

    operations = [
        migrations.RunSQL(
            sql="CREATE EXTENSION IF NOT EXISTS vector",
            reverse_sql="",
        ),
    ]
