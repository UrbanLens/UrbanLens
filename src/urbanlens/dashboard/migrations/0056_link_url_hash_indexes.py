"""Index each link table on its URL, which the Wayback archive looks every link naming a URL up by."""

from django.contrib.postgres.indexes import HashIndex
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0055_building_wikis_drop_wikipedia_seed"),
    ]

    operations = [
        migrations.AddIndex(model_name="pinlink", index=HashIndex(fields=["url"], name="db_plink_url_hash")),
        migrations.AddIndex(model_name="wikilink", index=HashIndex(fields=["url"], name="db_wlink_url_hash")),
    ]
