"""Protect existing profiles' "Want to Go" status label.

New profiles get it protected at seeding, like its four siblings (Visited,
Active, Abandoned, Demolished) always were. Without this, whether the label
can be deleted depends on when the account was created - and the "To Visit"
saved filter is built on it, so deleting it leaves that filter quietly
matching the wrong thing.

Only the default, profile-owned status label is touched: a tag or category a
user happens to have named the same thing is theirs to delete.
"""

from __future__ import annotations

from django.db import migrations


def protect(apps, schema_editor):
    """Set is_protected on every profile-owned "Want to Go" status label."""
    Label = apps.get_model("dashboard", "Label")
    Label.objects.filter(kind="status", name="Want to Go", profile__isnull=False, is_protected=False).update(is_protected=True)


def unprotect(apps, schema_editor):
    """Reverse: clear the flag again.

    Reversible on purpose - this migration only flips a boolean, so the
    reverse is exact rather than the silent no-op an irreversible data
    migration would leave behind.
    """
    Label = apps.get_model("dashboard", "Label")
    Label.objects.filter(kind="status", name="Want to Go", profile__isnull=False, is_protected=True).update(is_protected=False)


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0054_floorplan_relations")]
    operations = [migrations.RunPython(protect, unprotect)]
