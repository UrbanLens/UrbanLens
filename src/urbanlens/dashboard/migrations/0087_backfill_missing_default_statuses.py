from django.db import migrations

_DEFAULT_STATUSES = [
    {"name": "Visited",    "icon": "✅", "color": "#4CAF50", "order": 100, "is_protected": True},
    {"name": "Want to Go", "icon": "⭐", "color": "#2196F3", "order": 90,  "is_protected": False},
    {"name": "Active",     "icon": "🟢", "color": "#009688", "order": 80,  "is_protected": False},
    {"name": "Abandoned",  "icon": "🏚️", "color": "#FF9800", "order": 70,  "is_protected": False},
    {"name": "Demolished", "icon": "💀", "color": "#795548", "order": 60,  "is_protected": False},
]


def backfill_missing_statuses(apps, schema_editor):
    """Populate default statuses only for profiles that have none at all.

    Migration 0080 already backfilled all profiles that existed at that time,
    but profiles created between 0080 and the signals fix may have been missed.
    This pass is safe to re-run (get_or_create) and only touches profiles with
    zero status badges to minimise unnecessary work.
    """
    Badge = apps.get_model("dashboard", "Badge")
    Profile = apps.get_model("dashboard", "Profile")

    profiles_without_statuses = Profile.objects.exclude(
        id__in=Badge.objects.filter(kind="status").values("profile_id")
    )
    for profile in profiles_without_statuses:
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


def reverse_backfill(apps, schema_editor):
    """No-op reverse — we do not remove badges added by this migration."""


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0086_tripcomment_map_data"),
    ]

    operations = [
        migrations.RunPython(backfill_missing_statuses, reverse_backfill),
    ]
