"""The billing plugin's contributions reach core only while the plugin is installed and enabled."""

from __future__ import annotations

from unittest import mock

from django.test import Client, SimpleTestCase
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.plugins import plugin_available, plugin_registry
from urbanlens.UrbanLens.egress import BeatEgress, beat_classification, scheduled_beat_entries
from urbanlens.UrbanLens.settings.app import settings as app_settings

PLUGIN_BEAT_ENTRIES = {"stripe-subscriptions-sync", "pwyw-usage-ledger-sweep"}
CHECKOUT_ORIGIN = "https://checkout.stripe.com"


@mock.patch.object(app_settings, "disabled_plugins", [])
class PluginBeatTests(SimpleTestCase):
    def setUp(self) -> None:
        if not plugin_available("billing"):
            self.skipTest("billing plugin is not installed")

    def test_the_plugin_declares_and_classifies_its_beat_entries(self) -> None:
        self.assertEqual(set(plugin_registry.beat_schedule()), PLUGIN_BEAT_ENTRIES)
        classification = beat_classification()
        self.assertIs(classification["stripe-subscriptions-sync"], BeatEgress.EXTERNAL)
        self.assertIs(classification["pwyw-usage-ledger-sweep"], BeatEgress.INTERNAL)

    def test_outside_production_only_the_internal_entry_is_scheduled(self) -> None:
        entries = scheduled_beat_entries(plugin_registry.beat_schedule(), "staging", (), beat_classification())
        self.assertEqual(set(entries), {"pwyw-usage-ledger-sweep"})

    def test_production_schedules_both(self) -> None:
        entries = scheduled_beat_entries(plugin_registry.beat_schedule(), "production", (), beat_classification())
        self.assertEqual(set(entries), PLUGIN_BEAT_ENTRIES)

    def test_a_disabled_plugin_declares_no_beat_entries(self) -> None:
        with mock.patch.object(app_settings, "disabled_plugins", ["billing"]):
            self.assertEqual(plugin_registry.beat_schedule(), {})


@mock.patch.object(app_settings, "disabled_plugins", [])
class PluginEgressAndPolicyTests(SimpleTestCase):
    def setUp(self) -> None:
        if not plugin_available("billing"):
            self.skipTest("billing plugin is not installed")

    def test_stripe_is_an_unledgered_service_the_policy_knows(self) -> None:
        from urbanlens.dashboard.services.core.egress import explicit_category

        self.assertIsNotNone(explicit_category("stripe"))

    def test_the_policy_admits_stripe_checkout_for_form_posts(self) -> None:
        from django.conf import settings

        for policy_name in ("CONTENT_SECURITY_POLICY", "CONTENT_SECURITY_POLICY_REPORT_ONLY"):
            policy = getattr(settings, policy_name, None)
            if policy:
                self.assertIn(CHECKOUT_ORIGIN, policy["DIRECTIVES"]["form-action"], policy_name)


class MembershipSlotTests(TestCase):
    def test_the_settings_page_shows_membership_only_with_the_plugin(self) -> None:
        if not plugin_available("billing"):
            self.skipTest("billing plugin is not installed")
        user = baker.make("auth.User", is_active=True)
        client = Client()
        client.force_login(user)

        self.assertContains(client.get(reverse("settings.view")), "membership-settings-section")
        with mock.patch.object(app_settings, "disabled_plugins", ["billing"]):
            self.assertNotContains(client.get(reverse("settings.view")), "membership-settings-section")
