"""Migration: allow multiple ProfileNotes per (author, subject) pair.

Drops the unique_profile_note constraint so a viewer can keep
several notes about the same profile.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0099_alter_badge_kind_alter_friendinvitation_id_and_more"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="profilenote",
            options={"ordering": ["-created"]},
        ),
        migrations.RemoveConstraint(
            model_name="profilenote",
            name="unique_profile_note",
        ),
    ]
