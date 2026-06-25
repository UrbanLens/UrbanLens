"""Consolidate Pin.categories/tags/statuses into a single Pin.badges M2M field.

Data migration copies all existing rows from the three junction tables to the new
unified junction table, then removes the three old fields.
"""
from __future__ import annotations

from django.db import migrations, models


def pin_copy_to_badges(apps, schema_editor):
    """Copy pin-badge associations from the three old fields into badges."""
    Pin = apps.get_model("dashboard", "Pin")
    for pin in Pin.objects.prefetch_related("categories", "statuses", "tags").iterator(chunk_size=500):
        ids_to_add = set()
        ids_to_add.update(pin.categories.values_list("id", flat=True))
        ids_to_add.update(pin.statuses.values_list("id", flat=True))
        ids_to_add.update(pin.tags.values_list("id", flat=True))
        already_in = set(pin.badges.values_list("id", flat=True))
        to_add = ids_to_add - already_in
        if to_add:
            pin.badges.add(*to_add)

def location_copy_to_badges(apps, schema_editor):
    """Copy location-badge associations from the three old fields into badges."""
    Location = apps.get_model("dashboard", "Location")
    for location in Location.objects.prefetch_related("categories", "statuses", "tags").iterator(chunk_size=500):
        ids_to_add = set()
        ids_to_add.update(location.categories.values_list("id", flat=True))
        ids_to_add.update(location.statuses.values_list("id", flat=True))
        ids_to_add.update(location.tags.values_list("id", flat=True))
        already_in = set(location.badges.values_list("id", flat=True))
        to_add = ids_to_add - already_in
        if to_add:
            location.badges.add(*to_add)

def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0001_initial"),
    ]

    operations = [
        # Step 1: add the unified badges M2M (new junction table).
        migrations.AddField(
            model_name="pin",
            name="badges",
            field=models.ManyToManyField(
                blank=True,
                related_name="pins",
                to="dashboard.badge",
            ),
        ),
        # Step 2: copy existing rows into the new table.
        migrations.RunPython(pin_copy_to_badges, reverse_code=noop),
        # Step 3: remove the three old M2M fields.
        migrations.RemoveField(
            model_name="pin",
            name="categories",
        ),
        migrations.RemoveField(
            model_name="pin",
            name="statuses",
        ),
        migrations.RemoveField(
            model_name="pin",
            name="tags",
        ),
        # Step 4: add the unified badges M2M (new junction table).
        migrations.AddField(
            model_name="location",
            name="badges",
            field=models.ManyToManyField(
                blank=True,
                related_name="locations",
                to="dashboard.badge",
            ),
        ),
        # Step 5: copy existing rows into the new table.
        migrations.RunPython(location_copy_to_badges, reverse_code=noop),
        # Step 6: remove the three old M2M fields.
        migrations.RemoveField(
            model_name="location",
            name="categories",
        ),
        migrations.RemoveField(
            model_name="location",
            name="statuses",
        ),
        migrations.RemoveField(
            model_name="location",
            name="tags",
        ),
    ]
