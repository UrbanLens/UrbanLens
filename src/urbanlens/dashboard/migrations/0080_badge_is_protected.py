from django.db import migrations, models

_DEFAULT_STATUSES = [
    {"name": "Visited",    "icon": "✅", "color": "#4CAF50", "order": 100, "is_protected": True},
    {"name": "Want to Go", "icon": "⭐", "color": "#2196F3", "order": 90,  "is_protected": False},
    {"name": "Active",     "icon": "🟢", "color": "#009688", "order": 80,  "is_protected": False},
    {"name": "Abandoned",  "icon": "🏚️", "color": "#FF9800", "order": 70,  "is_protected": False},
    {"name": "Demolished", "icon": "💀", "color": "#795548", "order": 60,  "is_protected": False},
]


def backfill_status_badges(apps, schema_editor):
    """Create default status badges for every existing Profile.

    Mirrors what create_default_tags() does for new profiles.
    Uses get_or_create so the operation is safe to re-run.
    """
    Badge = apps.get_model("dashboard", "Badge")
    Profile = apps.get_model("dashboard", "Profile")

    for profile in Profile.objects.all():
        for d in _DEFAULT_STATUSES:
            Badge.objects.get_or_create(
                profile=profile,
                name=d["name"],
                kind="status",
                defaults={
                    "icon": d["icon"],
                    "color": d["color"],
                    "order": d["order"],
                    "is_protected": d["is_protected"],
                },
            )


def reverse_status_badges(apps, schema_editor):
    """Remove the default status badges created by backfill_status_badges.

    Only removes badges that still match the default values exactly;
    badges the user has already renamed or customised are left intact.
    """
    Badge = apps.get_model("dashboard", "Badge")
    default_names = {d["name"] for d in _DEFAULT_STATUSES}
    Badge.objects.filter(kind="status", name__in=default_names).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0079_pin_detail_circle_style"),
    ]

    operations = [
        migrations.AddField(
            model_name="badge",
            name="is_protected",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(backfill_status_badges, reverse_status_badges),
    ]
