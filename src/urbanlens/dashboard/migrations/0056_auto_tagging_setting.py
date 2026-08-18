"""Add the auto-tagging opt-out, and clear image caches an outage left empty.

Two unrelated changes share this migration because it had not been applied
anywhere when the second was written.

The cache half: while the SearXNG instance behind image search returned 403s,
the fetcher caught the failure and cached an empty result anyway. The
*existence* of a ``LocationCache`` row is what marks a source as having run, so
those pins kept "no photographs here" after the instance was fixed - nothing
refetches a source that already has a row. ``plugins.builtin.searxng_images``
no longer writes on failure, but that only helps future fetches; this clears
what the outage already left behind.
"""

from django.db import migrations, models

#: Matches ``SearxngImagesPanelSource.cache_source``.
_SOURCE = "searxng_images"


def clear_empty_image_caches(apps, schema_editor):
    """Delete empty ``searxng_images`` cache rows so they are fetched again.

    Deliberately narrow: only this source, and only rows with no results. A row
    with results is real data. A *genuine* empty result is indistinguishable
    from a cached outage at this distance, so the trade is re-running a cheap
    search for the pins that legitimately have no images, rather than leaving
    the ones that do permanently blank.
    """
    LocationCache = apps.get_model("dashboard", "LocationCache")
    # Filtered in Python rather than with a JSON lookup: `items` is absent on
    # rows written before that key existed and `[]` on the ones the outage
    # produced, and expressing "empty or missing" across JSONField lookups is
    # easy to get subtly wrong in a migration. One row per pin per source, so
    # reading them is cheap.
    doomed = [
        row_id
        for row_id, data in LocationCache.objects.filter(source=_SOURCE).values_list("id", "data").iterator(chunk_size=2000)
        if not (isinstance(data, dict) and data.get("items"))
    ]
    for start in range(0, len(doomed), 1000):
        LocationCache.objects.filter(id__in=doomed[start : start + 1000]).delete()


def keep_cleared_caches_cleared(apps, schema_editor):
    """Nothing to restore.

    The deleted rows held no information - that was the defect. Recreating them
    would re-assert "no photographs here", the state this clears. Written as a
    named function rather than ``RunPython.noop`` so the reverse is an explicit
    statement rather than an oversight (see the noop-reverse guard in
    ``test_migration_noop_reverse_guard.py``).
    """


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0055_protect_want_to_go"),
    ]

    operations = [
        migrations.AddField(
            model_name="profile",
            name="disable_auto_tagging",
            field=models.BooleanField(default=False, help_text="Turn off automatic tagging of your pins. Individual labels can also be excluded on the Organize page."),
        ),
        migrations.RunPython(clear_empty_image_caches, keep_cleared_caches_cleared),
    ]
