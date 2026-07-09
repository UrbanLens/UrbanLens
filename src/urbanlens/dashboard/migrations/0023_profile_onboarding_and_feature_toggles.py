"""Add the first-login welcome page flag and the Memories/Community/External-APIs toggles.

``welcome_onboarding_complete`` defaults to False so every profile created by the
signup signal after this migration shows /welcome/ once, with no signup-path
race. Existing accounts must never see that page, so this backfills them to
True; the other five fields need no backfill since their default (True, fully
featured) is already correct for existing accounts.
"""

from django.db import migrations, models


def backfill_existing_profiles_skip_welcome(apps, schema_editor):
    """Existing accounts have already "onboarded" - only new signups should see /welcome/."""
    Profile = apps.get_model("dashboard", "Profile")
    Profile.objects.update(welcome_onboarding_complete=True)


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0022_wiki_parent_wiki_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="profile",
            name="community_enabled",
            field=models.BooleanField(
                default=True,
                help_text="Allow other users to see your pins, profile, and friend requests.",
            ),
        ),
        migrations.AddField(
            model_name="profile",
            name="external_apis_enabled",
            field=models.BooleanField(
                default=True,
                help_text="Allow UrbanLens to call external services (weather, geocoding, place data, AI) on your behalf.",
            ),
        ),
        migrations.AddField(
            model_name="profile",
            name="track_geolocation",
            field=models.BooleanField(
                default=True, help_text="Record visits from your live device location."
            ),
        ),
        migrations.AddField(
            model_name="profile",
            name="track_pin_visits",
            field=models.BooleanField(
                default=True,
                help_text="Log visits to your pins from manual entries, imports, and photo tagging.",
            ),
        ),
        migrations.AddField(
            model_name="profile",
            name="track_routes",
            field=models.BooleanField(
                default=True, help_text="Save imported GPS routes/tracks."
            ),
        ),
        migrations.AddField(
            model_name="profile",
            name="welcome_onboarding_complete",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(backfill_existing_profiles_skip_welcome, migrations.RunPython.noop, elidable=True),
    ]
