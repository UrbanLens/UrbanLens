"""Add a URL slug to Trip and backfill existing rows.

Trips are private, so a sequential or predictable identifier in the URL
(e.g. "detroit-5") would hint at how many other trips exist. Slugs are
derived from the trip name with a random (not sequential) numeric suffix
on collision, mirroring ``PublicDashboardModel._generate_slug``.
"""

from __future__ import annotations

import random

from django.db import migrations, models


def _slugify_base(name: str | None, uuid_value) -> str:
    from django.utils.text import slugify

    return slugify(name) or str(uuid_value)


def backfill_trip_slugs(apps, schema_editor):
    Trip = apps.get_model("dashboard", "Trip")
    for trip in Trip.objects.filter(slug__isnull=True).iterator():
        base = _slugify_base(trip.name, trip.uuid)[:255]
        candidate = base
        while Trip.objects.filter(slug=candidate).exclude(pk=trip.pk).exists():
            suffix = f"-{random.randint(2, 90_000)}"  # noqa: S311 # nosec: B311 - not for cryptographic use
            candidate = base[: 255 - len(suffix)] + suffix
        Trip.objects.filter(pk=trip.pk).update(slug=candidate)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0030_trip_membership_last_viewed_at"),
    ]

    operations = [
        # Added unique from the start (rather than the usual add/backfill/alter
        # dance): Postgres allows multiple NULLs under a unique constraint, so
        # rows get it backfilled in place below without ever violating it.
        migrations.AddField(
            model_name="trip",
            name="slug",
            field=models.SlugField(max_length=255, null=True, blank=True, unique=True),
        ),
        migrations.RunPython(backfill_trip_slugs, noop),
    ]
