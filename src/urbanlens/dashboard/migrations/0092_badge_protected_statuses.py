"""Migration: remove global categories and mark more statuses as protected.

Changes:
1. Delete all global categories (profile=None, kind='category'). These were
   shared across all users; categories are now personal like tags and statuses.
2. Mark existing 'Active', 'Abandoned', and 'Demolished' status badges as
   protected - joining 'Visited' which was already protected.
"""

from django.db import migrations


def remove_global_categories(apps, schema_editor):
    """Delete all global (profile=None) category badges."""
    Badge = apps.get_model("dashboard", "Badge")
    Badge.objects.filter(profile=None, kind="category").delete()


def protect_default_statuses(apps, schema_editor):
    """Set is_protected=True on existing Active, Abandoned, and Demolished status badges."""
    Badge = apps.get_model("dashboard", "Badge")
    Badge.objects.filter(
        kind="status",
        name__in=["Active", "Abandoned", "Demolished"],
        is_protected=False,
    ).update(is_protected=True)


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0091_profile_uuid"),
    ]

    operations = [
        migrations.RunPython(remove_global_categories, migrations.RunPython.noop),
        migrations.RunPython(protect_default_statuses, migrations.RunPython.noop),
    ]
