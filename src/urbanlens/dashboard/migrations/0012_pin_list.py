# Generated migration for PinList model

from __future__ import annotations

from django.db import migrations, models
import django.db.models.deletion


def create_default_lists(apps, schema_editor):
    """Create 'Want to Go' and 'Visited' lists for all existing profiles."""
    Profile = apps.get_model("dashboard", "Profile")
    PinList = apps.get_model("dashboard", "PinList")

    for profile in Profile.objects.all():
        PinList.objects.get_or_create(
            profile=profile,
            name="Visited",
            defaults={"order": 0, "icon": "check_circle"},
        )
        PinList.objects.get_or_create(
            profile=profile,
            name="Want to Go",
            defaults={"order": 1, "icon": "schedule"},
        )


def remove_default_lists(apps, schema_editor):
    PinList = apps.get_model("dashboard", "PinList")
    PinList.objects.filter(name__in=["Visited", "Want to Go"]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0011_rename_dashboard_s_profile_idx_dashboard_s_profile_85d0eb_idx"),
    ]

    operations = [
        migrations.CreateModel(
            name="PinList",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=255)),
                ("description", models.TextField(blank=True, null=True)),
                ("order", models.IntegerField(default=0)),
                (
                    "icon",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("bookmark", "Bookmark"),
                            ("star", "Star"),
                            ("heart", "Heart"),
                            ("flag", "Flag"),
                            ("camera", "Camera"),
                            ("home", "Home"),
                            ("place", "Place"),
                            ("explore", "Explore"),
                            ("hiking", "Hiking"),
                            ("warning", "Warning"),
                            ("check_circle", "Check Circle"),
                            ("schedule", "Schedule"),
                            ("visibility", "Visibility"),
                            ("lock", "Private"),
                            ("archive", "Archive"),
                        ],
                        max_length=50,
                        null=True,
                    ),
                ),
                ("custom_icon", models.ImageField(blank=True, null=True, upload_to="pin_list_icons/")),
                (
                    "profile",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="pin_lists",
                        to="dashboard.profile",
                    ),
                ),
                (
                    "pins",
                    models.ManyToManyField(
                        blank=True,
                        related_name="pin_lists",
                        to="dashboard.pin",
                    ),
                ),
            ],
            options={
                "db_table": "dashboard_pin_lists",
                "ordering": ["-order", "name"],
                "abstract": False,
            },
        ),
        migrations.AddIndex(
            model_name="pinlist",
            index=models.Index(fields=["profile"], name="dashboard_pinlist_profile_idx"),
        ),
        migrations.AddIndex(
            model_name="pinlist",
            index=models.Index(fields=["profile", "order"], name="dashboard_pinlist_profile_order_idx"),
        ),
        migrations.RunPython(create_default_lists, remove_default_lists),
    ]
