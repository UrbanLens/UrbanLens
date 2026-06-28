"""Backfill the 'places' feature flag into any existing VIP subscription roles."""

from django.db import migrations


def _add_places_to_vip(apps, schema_editor):
    SubscriptionRole = apps.get_model("dashboard", "SubscriptionRole")
    for role in SubscriptionRole.objects.filter(slug="vip"):
        features = {f.strip() for f in (role.features or "").split(",") if f.strip()}
        if "places" not in features:
            features.add("places")
            role.features = ",".join(sorted(features))
            role.save(update_fields=["features"])


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0015_sitesettings_google_places_cache_days"),
    ]

    operations = [
        migrations.RunPython(_add_places_to_vip, migrations.RunPython.noop),
    ]
