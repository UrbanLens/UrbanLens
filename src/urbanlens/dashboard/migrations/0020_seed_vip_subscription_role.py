from django.db import migrations


def seed_vip_role(apps, schema_editor):
    """Create the historical hardcoded "vip" role as a real, admin-editable row.

    Previously seeded lazily by SubscriptionRole.ensure_defaults() on first
    request to the subscriptions admin page. That could not tell a fresh
    install apart from an admin who had deleted every role, so it kept
    silently recreating "vip" - moved here so seeding happens exactly once.
    ``get_or_create`` leaves an already-seeded (or admin-customized) row alone.
    """
    SubscriptionRole = apps.get_model("dashboard", "SubscriptionRole")
    SubscriptionRole.objects.get_or_create(
        slug="vip",
        defaults={
            "name": "VIP",
            "description": "Grants access to VIP-only features.",
            "features": "ai,beta_features,nearby_research,places,search",
            "storage_quota_gb": 500,
        },
    )


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0019_cost_tracking"),
    ]

    operations = [
        migrations.RunPython(
            code=seed_vip_role,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
