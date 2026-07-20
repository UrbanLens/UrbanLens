"""Invalidate cached Wikipedia matches so the new infobox field gets fetched.

Mirrors migrations 0082 and 0086 - LocationCache.get_fresh only compares a
time-based TTL, so a still-fresh "wikipedia" row never retriggers a refetch
on its own. WikipediaGateway.get_article_for_location now also fetches and
caches an "infobox" key (see _fetch_infobox in
services/apis/assets/wikipedia.py); without this, every match cached before
this migration would keep silently seeding articles with no infobox table
until its cache happened to expire naturally.
"""

from django.db import migrations


def invalidate_wikipedia_cache(apps, schema_editor):
    LocationCache = apps.get_model("dashboard", "LocationCache")
    LocationCache.objects.filter(source="wikipedia").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0088_cap_max_upload_file_size_at_clamd_stream_max_length"),
    ]

    operations = [
        migrations.RunPython(invalidate_wikipedia_cache, migrations.RunPython.noop),
    ]
