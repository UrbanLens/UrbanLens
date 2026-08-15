"""One link row per (pin, url) and per (wiki, url).

`services/locations/external_links.py` ran an exists()-then-create against no
constraint, from a LocationCache signal whose panel fetches run concurrently on
their own queue - so two panels contributing the same URL for one pin could both
miss and both insert.

The constraint hashes the URL rather than indexing it directly: `url` holds up to
2000 characters, and a btree entry over that in multibyte UTF-8 can exceed
Postgres' ~2704-byte row limit, which would turn a long link into an insert
error - a worse failure than the duplicate this prevents.

The data step keeps the lowest pk per group. Links have no dependent rows, so
the extras are deleted outright rather than merged; the surviving row is the one
every existing reader would already have returned via `.filter(...).first()`.
"""

from django.db import migrations, models
from django.db.models import F
from django.db.models.functions import MD5


def _drop_duplicate_links(model, owner_field: str) -> None:
    """Delete all but the lowest-pk row per (owner, url).

    Args:
        model: The historical PinLink or WikiLink model.
        owner_field: Name of the owning FK column - "pin" or "wiki".
    """
    seen: set[tuple[int, str]] = set()
    doomed: list[int] = []
    rows = model.objects.order_by("pk").values_list("pk", f"{owner_field}_id", "url")
    for pk, owner_id, url in rows.iterator():
        key = (owner_id, url)
        if key in seen:
            doomed.append(pk)
        else:
            seen.add(key)

    if doomed:
        model.objects.filter(pk__in=doomed).delete()


def drop_duplicate_links(apps, schema_editor):
    """Deduplicate both link tables ahead of their unique constraints.

    Args:
        apps: Historical app registry.
        schema_editor: Unused; required by the RunPython signature.
    """
    _drop_duplicate_links(apps.get_model("dashboard", "PinLink"), "pin")
    _drop_duplicate_links(apps.get_model("dashboard", "WikiLink"), "wiki")


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0046_trip_calendar_link_event_unique"),
    ]

    operations = [
        migrations.RunPython(drop_duplicate_links, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="pinlink",
            constraint=models.UniqueConstraint(F("pin"), MD5("url"), name="db_plink_pin_url_unique"),
        ),
        migrations.AddConstraint(
            model_name="wikilink",
            constraint=models.UniqueConstraint(F("wiki"), MD5("url"), name="db_wlink_wiki_url_unique"),
        ),
    ]
