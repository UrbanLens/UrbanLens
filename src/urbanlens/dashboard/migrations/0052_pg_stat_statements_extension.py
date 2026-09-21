from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0051_trip_comment_author_cascade"),
    ]

    operations = [
        # Succeeds whether or not the server preloads the library; only reading the view needs that
        # (docker-compose.yml's db command). Without the extension, X27's figures are reachable
        # only by running ALTER SYSTEM on one container by hand, which is how they were reached.
        migrations.RunSQL(
            sql="CREATE EXTENSION IF NOT EXISTS pg_stat_statements",
            reverse_sql="",
        ),
    ]
