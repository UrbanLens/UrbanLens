# Generated manually for subscription role support.

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def seed_vip_role(apps, schema_editor):
    SubscriptionRole = apps.get_model("dashboard", "SubscriptionRole")
    SubscriptionRole.objects.get_or_create(
        slug="vip",
        defaults={
            "name": "VIP",
            "description": "Grants access to AI-assisted features.",
            "features": "ai",
        },
    )


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SubscriptionRole",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("slug", models.CharField(db_index=True, max_length=50, unique=True)),
                ("name", models.CharField(max_length=100)),
                ("description", models.CharField(blank=True, max_length=255)),
                ("features", models.CharField(blank=True, help_text="Comma-separated SiteFeature values.", max_length=500)),
            ],
            options={"ordering": ["name"]},
        ),
        migrations.CreateModel(
            name="UserSubscription",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("expires_at", models.DateTimeField(blank=True, null=True)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                ("granted_by", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="granted_subscriptions", to=settings.AUTH_USER_MODEL)),
                ("role", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="user_subscriptions", to="dashboard.subscriptionrole")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="subscriptions", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-created"]},
        ),
        migrations.CreateModel(
            name="PendingSubscriptionGrant",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("duration_months", models.CharField(blank=True, help_text="Blank means indefinite.", max_length=20)),
                ("granted_by", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="pending_subscription_grants", to=settings.AUTH_USER_MODEL)),
                ("invitation", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="pending_subscription_grants", to="dashboard.friendinvitation")),
                ("role", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="pending_grants", to="dashboard.subscriptionrole")),
            ],
            options={"ordering": ["-created"]},
        ),
        migrations.AddConstraint(
            model_name="usersubscription",
            constraint=models.UniqueConstraint(condition=models.Q(("revoked_at__isnull", True)), fields=("user", "role"), name="unique_active_user_subscription_role"),
        ),
        migrations.RunPython(seed_vip_role, migrations.RunPython.noop),
    ]
