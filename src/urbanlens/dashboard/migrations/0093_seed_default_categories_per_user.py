"""Migration: seed default categories for all existing user profiles.

The concept of global (profile=None) categories was removed in 0092. This
migration recreates those categories as personal badges on every existing
profile so that no user is left without a starter set. New users receive the
same set via the create_default_tags signal (badges/signals.py).

Users may freely rename, recolor, or delete any of these categories after
creation - they are not protected.
"""

from django.db import migrations

# Keep this list in sync with DEFAULT_CATEGORIES in
# dashboard/models/badges/signals.py and the keys of CATEGORY_PATTERNS in
# dashboard/services/ai/keywords.py.
_DEFAULT_CATEGORIES: list[str] = [
    "Airport",
    "Amusement Park",
    "Asylum",
    "Bank",
    "Bridge",
    "Bunker",
    "Cars",
    "Castle",
    "Church",
    "Factory",
    "Fire Tower",
    "Firehouse",
    "Funeral Home",
    "Graveyard",
    "Hospital",
    "Hotel",
    "House",
    "Laboratory",
    "Library",
    "Lighthouse",
    "Mall",
    "Mansion",
    "Military Base",
    "Monument",
    "Police Station",
    "Power Plant",
    "Prison",
    "Resort",
    "Ruins",
    "School",
    "Stadium",
    "Theater",
    "Traincar",
    "Train Station",
    "Tunnel",
]


def seed_default_categories(apps, schema_editor):
    """Create missing default category badges for every existing profile."""
    Badge = apps.get_model("dashboard", "Badge")
    Profile = apps.get_model("dashboard", "Profile")

    total = len(_DEFAULT_CATEGORIES)
    for profile in Profile.objects.all():
        for i, name in enumerate(_DEFAULT_CATEGORIES):
            Badge.objects.get_or_create(
                profile=profile,
                name=name,
                kind="category",
                defaults={"order": total - i},
            )


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0092_badge_protected_statuses"),
    ]

    operations = [
        migrations.RunPython(seed_default_categories, migrations.RunPython.noop),
    ]
