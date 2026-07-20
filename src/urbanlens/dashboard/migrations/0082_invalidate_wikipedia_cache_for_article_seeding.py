"""Drop cached "wikipedia" LocationCache rows so they refetch and seed wiki articles.

New behavior: whenever a Wikipedia article is (re)cached for a location, its
wiki's article (if it doesn't have one yet) is seeded from it - see
services.wiki_seed/models.cache.signals. A location whose Wikipedia match was
already cached *before* this feature existed would otherwise never refetch
(LocationCache.get_fresh only compares against a time-based TTL, and a
still-fresh row is never rewritten) and so would never trigger seeding. A
missing row is "never queried" per LocationCache's own docstring, so deleting
these rows is enough to make the next panel view/enrichment pass do a fresh
lookup and (for a location with a wiki and no article yet) seed it.
"""

from __future__ import annotations

from django.db import migrations


def invalidate(apps, schema_editor):
    """Delete every "wikipedia" LocationCache row so it gets refetched."""
    LocationCache = apps.get_model("dashboard", "LocationCache")
    LocationCache.objects.filter(source="wikipedia").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0081_remove_property_jurisdiction"),
    ]

    operations = [
        migrations.RunPython(invalidate, migrations.RunPython.noop),
    ]
