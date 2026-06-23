"""Replace dark_mode boolean with theme_mode choice; add contact fields to Profile."""

from __future__ import annotations

from django.db import migrations, models


def migrate_dark_mode_to_theme(apps, schema_editor):
    """Convert existing dark_mode boolean to the new theme_mode varchar."""
    Profile = apps.get_model("dashboard", "Profile")
    # dark_mode=True → 'dark'; dark_mode=False → 'light' (user had explicitly picked light)
    Profile.objects.filter(dark_mode=True).update(theme_mode="dark")
    Profile.objects.filter(dark_mode=False).update(theme_mode="light")


class Migration(migrations.Migration):
    """Add theme_mode + contact fields; remove legacy dark_mode boolean."""

    dependencies = [("dashboard", "0003_slugs")]

    operations = [
        # ── 1. Add theme_mode (nullable first so existing rows are valid) ─────
        migrations.AddField(
            model_name="profile",
            name="theme_mode",
            field=models.CharField(
                blank=True,
                choices=[("system", "System (follows your OS)"), ("light", "Light"), ("dark", "Dark")],
                default="system",
                max_length=10,
            ),
        ),
        # ── 2. Populate theme_mode from dark_mode ────────────────────────────
        migrations.RunPython(migrate_dark_mode_to_theme, migrations.RunPython.noop),
        # ── 3. Remove legacy dark_mode ───────────────────────────────────────
        migrations.RemoveField(model_name="profile", name="dark_mode"),
        # ── 4. Contact fields ─────────────────────────────────────────────────
        migrations.AddField(
            model_name="profile",
            name="phone_number",
            field=models.CharField(blank=True, default="", max_length=30),
        ),
        migrations.AddField(
            model_name="profile",
            name="signal_username",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="profile",
            name="discord_username",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="profile",
            name="whatsapp_number",
            field=models.CharField(blank=True, default="", max_length=30),
        ),
        migrations.AddField(
            model_name="profile",
            name="telegram_username",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="profile",
            name="matrix_handle",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AddField(
            model_name="profile",
            name="contact_visibility",
            field=models.CharField(
                choices=[
                    ("anyone", "Anyone"),
                    ("friends", "Friends Only"),
                    ("common_pin", "Users with a pin in common"),
                    ("common_friend", "Users with a friend in common"),
                    ("common_trip", "Users with a trip in common"),
                    ("no_one", "No one"),
                ],
                default="friends",
                help_text="Who can see your contact methods (phone, Signal, Discord, etc.).",
                max_length=20,
            ),
        ),
    ]
