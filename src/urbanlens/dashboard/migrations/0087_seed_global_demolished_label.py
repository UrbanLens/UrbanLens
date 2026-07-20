"""Seed one canonical global "Demolished" status label, for Wikis.

Every Profile already gets its own private, protected "Demolished" status
label (``models.labels.signals.create_default_tags``) for tagging pins. A
Wiki has no owning profile - its ``labels`` M2M is explicitly shared taxonomy,
visible to (and editable by) every user who can see the location - so a
per-profile label doesn't fit there. This seeds one global (``profile=None``)
row with the same values as the per-profile default, so
``services.labels.add_demolished_status_to_wiki`` has a single, deterministic
row to attach rather than having to pick one arbitrary user's private copy.
"""

from __future__ import annotations

from django.db import migrations


def seed_global_demolished_label(apps, schema_editor):
    Label = apps.get_model("dashboard", "Label")
    Label.objects.get_or_create(
        profile=None,
        name="Demolished",
        kind="status",
        defaults={
            "icon": "💀",
            "color": "#795548",
            "order": 60,
            "is_protected": True,
        },
    )


def remove_global_demolished_label(apps, schema_editor):
    Label = apps.get_model("dashboard", "Label")
    Label.objects.filter(profile=None, name="Demolished", kind="status").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0089_invalidate_wikipedia_cache_for_infobox_seeding"),
    ]

    operations = [
        migrations.RunPython(seed_global_demolished_label, remove_global_demolished_label),
    ]
