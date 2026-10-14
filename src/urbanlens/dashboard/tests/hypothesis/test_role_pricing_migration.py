"""Migration 0077 moves each role's pricing into RolePricing, and back, without losing a value."""

from __future__ import annotations

from unittest import skipUnless

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from urbanlens.dashboard.plugins import plugin_available

BEFORE = [("dashboard", "0076_place_merges")]
AFTER = [("dashboard", "0077_role_pricing_out_of_core")]
PRICING = {
    "monthly_price_cents": 500,
    "pay_what_you_want": True,
    "pwyw_dynamic_threshold": False,
    "pwyw_minimum_cents": 100,
    "stripe_product_id": "prod_migrated",
}


def _migrate(targets: list[tuple[str, str]]):
    executor = MigrationExecutor(connection)
    executor.migrate(targets)
    executor.loader.build_graph()
    return executor.loader.project_state(targets).apps


@skipUnless(plugin_available("billing"), "RolePricing is the billing plugin's model")
class RolePricingMigrationTests(TransactionTestCase):
    def tearDown(self) -> None:
        _migrate(AFTER)

    def test_pricing_moves_out_of_the_role_and_back(self) -> None:
        old_apps = _migrate(BEFORE)
        role = old_apps.get_model("dashboard", "SubscriptionRole").objects.create(
            slug="migrated-role", name="Migrated role", **PRICING
        )

        new_apps = _migrate(AFTER)
        pricing = new_apps.get_model("dashboard", "RolePricing").objects.get(role_id=role.pk)
        self.assertEqual({field: getattr(pricing, field) for field in PRICING}, PRICING)

        old_apps = _migrate(BEFORE)
        restored = old_apps.get_model("dashboard", "SubscriptionRole").objects.get(pk=role.pk)
        self.assertEqual({field: getattr(restored, field) for field in PRICING}, PRICING)
