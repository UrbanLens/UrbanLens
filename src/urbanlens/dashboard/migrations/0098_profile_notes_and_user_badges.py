"""Add ProfileNote, ProfileBadgeAssignment; add KIND_USER to Badge choices; seed global user badges."""

import django.db.models.deletion
from django.db import migrations, models


def _seed_user_badges(apps, schema_editor):
    Badge = apps.get_model("dashboard", "Badge")
    defaults = [
        {"name": "Preservation", "icon": "🌿", "color": "#4CAF50", "order": 40, "is_protected": True},
        {"name": "Vandalism",    "icon": "⚠️",  "color": "#F44336", "order": 30, "is_protected": True},
        {"name": "Photography",  "icon": "📷",  "color": "#2196F3", "order": 20, "is_protected": True},
        {"name": "Influencer",   "icon": "📣",  "color": "#9C27B0", "order": 10, "is_protected": True},
    ]
    for d in defaults:
        Badge.objects.get_or_create(
            profile=None,
            name=d["name"],
            kind="user",
            defaults={k: v for k, v in d.items() if k != "name"},
        )


def _remove_user_badges(apps, schema_editor):
    Badge = apps.get_model("dashboard", "Badge")
    Badge.objects.filter(kind="user", profile__isnull=True, is_protected=True).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0097_friendinvitation"),
    ]

    operations = [
        # ProfileNote
        migrations.CreateModel(
            name="ProfileNote",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("content", models.TextField(blank=True, default="")),
                (
                    "author",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="authored_profile_notes",
                        to="dashboard.profile",
                    ),
                ),
                (
                    "subject",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="received_profile_notes",
                        to="dashboard.profile",
                    ),
                ),
            ],
            options={"abstract": False},
        ),
        migrations.AddConstraint(
            model_name="profilenote",
            constraint=models.UniqueConstraint(fields=["author", "subject"], name="unique_profile_note"),
        ),
        # ProfileBadgeAssignment
        migrations.CreateModel(
            name="ProfileBadgeAssignment",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                (
                    "author",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="profile_badge_assignments",
                        to="dashboard.profile",
                    ),
                ),
                (
                    "subject",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="received_profile_badge_assignments",
                        to="dashboard.profile",
                    ),
                ),
                (
                    "badge",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="profile_assignments",
                        to="dashboard.badge",
                    ),
                ),
            ],
            options={"abstract": False},
        ),
        migrations.AddConstraint(
            model_name="profilebadgeassignment",
            constraint=models.UniqueConstraint(
                fields=["author", "subject", "badge"],
                name="unique_profile_badge_assignment",
            ),
        ),
        # Seed global user-type badges
        migrations.RunPython(_seed_user_badges, _remove_user_badges),
    ]
