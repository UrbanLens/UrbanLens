"""
Move SubscriptionRole's prices and pay-what-you-want rules into RolePricing.

The billing plugin owns the pricing rules, so core SubscriptionRole keeps none of them. Values are copied
before the old columns are dropped, and copied back on reverse.
"""

import django.core.validators
from django.db import migrations, models
import django.db.models.deletion

PRICING_FIELDS = ("monthly_price_cents", "pay_what_you_want", "pwyw_dynamic_threshold", "pwyw_minimum_cents", "stripe_product_id")


def copy_pricing_out(apps, schema_editor):
    SubscriptionRole = apps.get_model("dashboard", "SubscriptionRole")
    RolePricing = apps.get_model("dashboard", "RolePricing")
    RolePricing.objects.bulk_create([RolePricing(role_id=role.pk, **{field: getattr(role, field) for field in PRICING_FIELDS}) for role in SubscriptionRole.objects.all()])


def copy_pricing_back(apps, schema_editor):
    SubscriptionRole = apps.get_model("dashboard", "SubscriptionRole")
    RolePricing = apps.get_model("dashboard", "RolePricing")
    for pricing in RolePricing.objects.all():
        SubscriptionRole.objects.filter(pk=pricing.role_id).update(**{field: getattr(pricing, field) for field in PRICING_FIELDS})


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0076_place_merges"),
    ]

    operations = [
        migrations.CreateModel(
            name="RolePricing",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                (
                    "monthly_price_cents",
                    models.IntegerField(
                        blank=True,
                        help_text="Fixed monthly price in cents, e.g. 500 for $5.00. Blank means not offered at a fixed price.",
                        null=True,
                        validators=[django.core.validators.MinValueValidator(0)],
                    ),
                ),
                (
                    "pay_what_you_want",
                    models.BooleanField(
                        default=False,
                        help_text="Let users choose their own monthly pledge instead of (or as well as) the fixed price above.",
                    ),
                ),
                (
                    "pwyw_dynamic_threshold",
                    models.BooleanField(
                        default=False,
                        help_text="Requires pay_what_you_want. Grants this role only in months the user's pledge meets or exceeds the site's current cost-per-user (services.admin.cost_tracking.cost_per_user), recalculated at each successful charge - not a fixed minimum.",
                    ),
                ),
                (
                    "pwyw_minimum_cents",
                    models.IntegerField(
                        blank=True,
                        help_text="Static minimum pledge (cents) required to hold this role when pay_what_you_want is on and pwyw_dynamic_threshold is off. Blank or 0 means any pledge grants it.",
                        null=True,
                        validators=[django.core.validators.MinValueValidator(0)],
                    ),
                ),
                (
                    "stripe_product_id",
                    models.CharField(
                        blank=True,
                        help_text="Stripe Product id for this role, created lazily on first checkout. Prices are generated inline per checkout/pledge instead of being pre-created, so editing monthly_price_cents needs no Stripe sync.",
                        max_length=255,
                    ),
                ),
                (
                    "role",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="pricing",
                        to="dashboard.subscriptionrole",
                    ),
                ),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.RunPython(copy_pricing_out, copy_pricing_back),
        migrations.RemoveField(model_name="subscriptionrole", name="monthly_price_cents"),
        migrations.RemoveField(model_name="subscriptionrole", name="pay_what_you_want"),
        migrations.RemoveField(model_name="subscriptionrole", name="pwyw_dynamic_threshold"),
        migrations.RemoveField(model_name="subscriptionrole", name="pwyw_minimum_cents"),
        migrations.RemoveField(model_name="subscriptionrole", name="stripe_product_id"),
    ]
