"""Migration: clear is_protected on all kind='user' (People) badges.

Migration 0098 seeded the global People badges (Preservation, Vandalism,
Photography, Influencer) with is_protected=True, which prevented users from
editing or deleting them on the Organize page. People badges are personal
labels and should always be fully editable - only status badges (Visited,
Active, etc.) are intentionally protected.
"""

from django.db import migrations


def unprotect_user_badges(apps, schema_editor):
    """Remove protected flag from all kind='user' badges."""
    Badge = apps.get_model("dashboard", "Badge")
    Badge.objects.filter(kind="user", is_protected=True).update(is_protected=False)


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0104_merge_20260621_2207"),
    ]

    operations = [
        migrations.RunPython(unprotect_user_badges, migrations.RunPython.noop),
    ]
