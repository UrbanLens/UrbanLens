"""Add URL slugs to Pin, Profile, and Location.

Schema migration: adds nullable slug fields.
Data migration: populates slugs from existing names / usernames.
After this migration every record has a slug; new records get one auto-generated
by each model's save() method.
"""

from __future__ import annotations

from collections import defaultdict

from django.db import migrations, models
from django.utils.text import slugify


def _unique_slug(base: str, used: set) -> str:
    """Return `base` (or `base-N`) such that the result is not in `used`."""
    if not base:
        base = "item"
    candidate = base
    n = 2
    while candidate in used:
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def populate_pin_slugs(apps, schema_editor):
    """Assign a per-profile-unique slug to every Pin."""
    Pin = apps.get_model("dashboard", "Pin")

    pins_data = list(
        Pin.objects.values("id", "profile_id", "nickname", "location__name")
    )

    by_profile: dict = defaultdict(list)
    for p in pins_data:
        by_profile[p["profile_id"]].append(p)

    for _profile_id, pins in by_profile.items():
        used: set = set()
        for pin in pins:
            raw = pin["nickname"] or pin["location__name"] or ""
            base = slugify(raw)[:200] or "pin"
            slug = _unique_slug(base, used)
            used.add(slug)
            Pin.objects.filter(pk=pin["id"]).update(slug=slug)


def populate_profile_slugs(apps, schema_editor):
    """Assign a globally-unique slug to every Profile."""
    Profile = apps.get_model("dashboard", "Profile")

    profiles = list(Profile.objects.values("id", "user__username"))
    used: set = set()
    for profile in profiles:
        base = slugify(profile["user__username"] or "")[:150] or "user"
        slug = _unique_slug(base, used)
        used.add(slug)
        Profile.objects.filter(pk=profile["id"]).update(slug=slug)


def populate_location_slugs(apps, schema_editor):
    """Assign a globally-unique slug to every Location."""
    Location = apps.get_model("dashboard", "Location")

    locations = list(Location.objects.values("id", "name"))
    used: set = set()
    for location in locations:
        base = slugify(location["name"] or "")[:255] or "location"
        slug = _unique_slug(base, used)
        used.add(slug)
        Location.objects.filter(pk=location["id"]).update(slug=slug)


class Migration(migrations.Migration):
    """Add nullable slug fields and populate them from existing data."""

    dependencies = [("dashboard", "0002_profile_setup_complete")]

    operations = [
        # ── Schema: add slug columns ─────────────────────────────────────────
        migrations.AddField(
            model_name="pin",
            name="slug",
            field=models.SlugField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="profile",
            name="slug",
            field=models.SlugField(blank=True, max_length=150, null=True, unique=True),
        ),
        migrations.AddField(
            model_name="location",
            name="slug",
            field=models.SlugField(blank=True, max_length=255, null=True, unique=True),
        ),
        # ── Data: fill slugs for existing rows ──────────────────────────────
        migrations.RunPython(populate_pin_slugs, migrations.RunPython.noop),
        migrations.RunPython(populate_profile_slugs, migrations.RunPython.noop),
        migrations.RunPython(populate_location_slugs, migrations.RunPython.noop),
        # ── Constraints: enforce per-profile uniqueness for Pin.slug ─────────
        migrations.AddConstraint(
            model_name="pin",
            constraint=models.UniqueConstraint(
                condition=models.Q(slug__isnull=False),
                fields=["profile", "slug"],
                name="dashboard_pin_unique_slug_per_profile",
            ),
        ),
    ]
