"""Pages render in an install without the billing plugin, where its URL names do not exist."""

from __future__ import annotations

from unittest import mock

from django.test import Client
from django.urls import NoReverseMatch, reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.UrbanLens.settings.app import settings as app_settings

PLUGIN_ROUTE_NAMES = frozenset(
    {
        "site_admin_subscriptions",
        "settings.billing",
        "settings.billing.checkout",
        "settings.billing.portal",
        "settings.billing.pledge",
        "settings.billing.cancel",
        "billing.stripe_webhook",
    }
)


def _reverse_without_plugin_routes(view_name: str, *args, **kwargs) -> str:
    if view_name in PLUGIN_ROUTE_NAMES:
        raise NoReverseMatch(f"'{view_name}' belongs to a plugin that is not installed")
    return reverse(view_name, *args, **kwargs)


class PagesWithoutBillingTests(TestCase):
    def test_admin_and_settings_pages_render_when_billing_routes_are_absent(self) -> None:
        user = baker.make("auth.User", is_superuser=True, is_staff=True)
        client = Client()
        client.force_login(user)

        with (
            mock.patch.object(app_settings, "disabled_plugins", ["billing"]),
            # The url tag imports reverse inside render(), so patching the module attribute reaches it.
            mock.patch("django.urls.reverse", side_effect=_reverse_without_plugin_routes),
        ):
            for name in ("site_admin", "settings.view"):
                with self.subTest(page=name):
                    response = client.get(reverse(name))
                    self.assertEqual(response.status_code, 200)
