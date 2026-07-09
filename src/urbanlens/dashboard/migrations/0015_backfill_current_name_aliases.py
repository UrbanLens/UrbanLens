"""Backfill alias rows for current names.

The alias list is now the full set of names a pin or place is known by,
*including* the current one (the Pin/Wiki ``save()`` overrides maintain this
invariant going forward). This migration seeds that invariant for existing
rows: every meaningful pin name, wiki name, and location official name gets
an alias row. Unique constraints absorb rows that already exist.
"""

from django.db import migrations

# ``is_meaningful_name`` is a pure function over its string argument (no model
# or settings access), so importing it into a data migration is safe; it is
# pinned here by name and a rename would surface as an ImportError in CI.
from urbanlens.dashboard.services.locations.naming import is_meaningful_name

BATCH_SIZE = 500


def backfill_current_name_aliases(apps, schema_editor):
    """Create alias rows for every meaningful current name."""
    Pin = apps.get_model("dashboard", "Pin")
    PinAlias = apps.get_model("dashboard", "PinAlias")
    Wiki = apps.get_model("dashboard", "Wiki")
    WikiAlias = apps.get_model("dashboard", "WikiAlias")
    Location = apps.get_model("dashboard", "Location")

    pin_aliases = [
        PinAlias(pin_id=pin_id, name=name.strip(), kind="alternate", source="user" if user_provided else "other")
        for pin_id, name, user_provided in Pin.objects.exclude(name__isnull=True).exclude(name="").values_list("id", "name", "name_is_user_provided").iterator()
        if is_meaningful_name((name or "").strip())
    ]
    PinAlias.objects.bulk_create(pin_aliases, batch_size=BATCH_SIZE, ignore_conflicts=True)

    wiki_aliases = [
        WikiAlias(wiki_id=wiki_id, name=name.strip(), kind="alternate", source="other")
        for wiki_id, name in Wiki.objects.exclude(name="").values_list("id", "name").iterator()
        if is_meaningful_name((name or "").strip())
    ]
    WikiAlias.objects.bulk_create(wiki_aliases, batch_size=BATCH_SIZE, ignore_conflicts=True)

    official_aliases = [
        WikiAlias(wiki_id=wiki_id, name=official_name.strip(), kind="official", source="other")
        for wiki_id, official_name in Location.objects.filter(wiki__isnull=False).exclude(official_name__isnull=True).exclude(official_name="").values_list("wiki__id", "official_name").iterator()
        if is_meaningful_name((official_name or "").strip())
    ]
    WikiAlias.objects.bulk_create(official_aliases, batch_size=BATCH_SIZE, ignore_conflicts=True)


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0014_sitesettings_default_name_source_priority_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill_current_name_aliases, migrations.RunPython.noop, elidable=True),
    ]
