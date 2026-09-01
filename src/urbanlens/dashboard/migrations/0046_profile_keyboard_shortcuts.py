"""Per-user keyboard shortcut overrides, for the new Settings > Shortcuts section.

See ``models.profile.model.Profile.keyboard_shortcuts`` and
``frontend/ts/shared/hotkeys.ts``.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    """Add Profile.keyboard_shortcuts."""

    dependencies = [
        ("dashboard", "0045_image_exif_metadata_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="profile",
            name="keyboard_shortcuts",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
