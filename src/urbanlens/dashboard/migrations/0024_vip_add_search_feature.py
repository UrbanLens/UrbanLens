"""Backfill the 'search' feature flag into any existing VIP subscription roles."""

from django.db import migrations


def _add_search_to_vip(apps, schema_editor):
    SubscriptionRole = apps.get_model("dashboard", "SubscriptionRole")
    for role in SubscriptionRole.objects.filter(slug="vip"):
        features = {f.strip() for f in (role.features or "").split(",") if f.strip()}
        if "search" not in features:
            features.add("search")
            role.features = ",".join(sorted(features))
            role.save(update_fields=["features"])


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0023_pin_sharing"),
    ]

    operations = [
        migrations.RunPython(_add_search_to_vip, migrations.RunPython.noop),
    ]
