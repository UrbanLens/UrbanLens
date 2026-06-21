"""Replace per-column social links on Profile with a normalised SocialLink table.

Twitch data is intentionally not migrated (platform replaced by TikTok).
"""

from django.db import migrations, models
import django.db.models.deletion

# Fields on Profile → platform key in SocialLink.  twitch excluded.
_PROFILE_FIELD_TO_PLATFORM = [
    ("instagram", "instagram"),
    ("discord", "discord"),
    ("bluesky", "bluesky"),
    ("uer_id", "uer"),
    ("facebook", "facebook"),
    ("flickr", "flickr"),
    ("youtube", "youtube"),
    ("website", "website"),
    ("reddit", "reddit"),
]


def migrate_links_forward(apps, schema_editor):
    """Copy social link columns from Profile rows into SocialLink rows."""
    Profile = apps.get_model("dashboard", "Profile")
    SocialLink = apps.get_model("dashboard", "SocialLink")
    for profile in Profile.objects.all():
        for field_name, platform in _PROFILE_FIELD_TO_PLATFORM:
            handle = getattr(profile, field_name, None)
            if handle:
                SocialLink.objects.get_or_create(
                    profile=profile,
                    platform=platform,
                    defaults={"handle": handle},
                )


def migrate_links_backward(apps, schema_editor):
    """Restore social link columns on Profile from SocialLink rows."""
    apps.get_model("dashboard", "Profile")
    SocialLink = apps.get_model("dashboard", "SocialLink")
    platform_to_field = {plat: field for field, plat in _PROFILE_FIELD_TO_PLATFORM}
    for link in SocialLink.objects.select_related("profile"):
        field_name = platform_to_field.get(link.platform)
        if field_name:
            setattr(link.profile, field_name, link.handle)
            link.profile.save(update_fields=[field_name])


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0009_profile_reddit"),
    ]

    operations = [
        # 1. Create the new table.
        migrations.CreateModel(
            name="SocialLink",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("platform", models.CharField(max_length=30)),
                ("handle", models.CharField(max_length=500)),
                (
                    "profile",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="social_links",
                        to="dashboard.profile",
                    ),
                ),
            ],
            options={
                "db_table": "dashboard_social_links",
            },
        ),
        migrations.AddConstraint(
            model_name="sociallink",
            constraint=models.UniqueConstraint(
                fields=["profile", "platform"],
                name="social_link_unique_profile_platform",
            ),
        ),
        migrations.AddIndex(
            model_name="sociallink",
            index=models.Index(fields=["profile"], name="dashboard_s_profile_idx"),
        ),

        # 2. Copy existing data.
        migrations.RunPython(migrate_links_forward, migrate_links_backward),

        # 3. Drop the old columns from Profile (twitch included).
        migrations.RemoveField(model_name="profile", name="instagram"),
        migrations.RemoveField(model_name="profile", name="discord"),
        migrations.RemoveField(model_name="profile", name="bluesky"),
        migrations.RemoveField(model_name="profile", name="uer_id"),
        migrations.RemoveField(model_name="profile", name="facebook"),
        migrations.RemoveField(model_name="profile", name="flickr"),
        migrations.RemoveField(model_name="profile", name="youtube"),
        migrations.RemoveField(model_name="profile", name="twitch"),
        migrations.RemoveField(model_name="profile", name="website"),
        migrations.RemoveField(model_name="profile", name="reddit"),
    ]
