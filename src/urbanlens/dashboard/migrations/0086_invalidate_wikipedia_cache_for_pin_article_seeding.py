"""Drop cached "wikipedia" LocationCache rows so they refetch and seed pin articles.

New behavior: whenever a Wikipedia article is (re)cached for a location,
every pin at that location (whose owner has
Profile.auto_create_pin_article_from_wikipedia on) gets its article seeded
from it, if it doesn't have one yet - see services.wiki_seed/
models.cache.signals. A location whose Wikipedia match was already cached
*before* this feature existed would otherwise never refetch
(LocationCache.get_fresh only compares against a time-based TTL, and a
still-fresh row is never rewritten) and so would never trigger seeding for
its pins. This is the same mechanism (and the same rationale) as migration
0082, which did this once already for the wiki-side equivalent - run again
now that pins are also seeded, since most caches invalidated by 0082 have
already refetched and gone fresh again in the time since.
"""

from __future__ import annotations

from django.db import migrations


def invalidate(apps, schema_editor):
    """Delete every "wikipedia" LocationCache row so it gets refetched."""
    LocationCache = apps.get_model("dashboard", "LocationCache")
    LocationCache.objects.filter(source="wikipedia").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0085_auto_create_pin_article_from_wikipedia"),
    ]

    operations = [
        migrations.RunPython(invalidate, migrations.RunPython.noop),
    ]
