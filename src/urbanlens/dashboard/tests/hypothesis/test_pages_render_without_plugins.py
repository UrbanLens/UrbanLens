"""Pages render in an install that carries none of the package plugins, where their routes and fragments do not exist."""

from __future__ import annotations

from contextlib import ExitStack
from unittest import mock

from django.test import Client
from django.urls import NoReverseMatch, URLPattern, URLResolver, reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.plugins import points
from urbanlens.dashboard.plugins.registry import plugin_registry
from urbanlens.dashboard.plugins.testing import contributed, plugin_withdrawn

PAGES = ("site_admin_home", "site_admin", "site_admin_subscriptions", "settings.view", "about", "faq")


def _route_names(pattern: URLPattern | URLResolver) -> set[str]:
    if isinstance(pattern, URLPattern):
        return {pattern.name} if pattern.name else set()
    return {name for child in pattern.url_patterns for name in _route_names(child)}


class PagesWithoutPluginsTests(TestCase):
    def test_pages_render_when_every_package_plugin_is_withdrawn(self) -> None:
        packages = [info.name for info in plugin_registry.plugins() if info.is_active and info.source.is_package]
        route_names = {
            name
            for plugin in packages
            for pattern in contributed(plugin, points.ROOT_URLS)
            for name in _route_names(pattern)
        }

        def reverse_without_plugin_routes(view_name: str, *args, **kwargs) -> str:
            if view_name in route_names:
                raise NoReverseMatch(f"'{view_name}' belongs to a plugin that is not installed")
            return reverse(view_name, *args, **kwargs)

        user = baker.make("auth.User", is_superuser=True, is_staff=True)
        client = Client()
        client.force_login(user)

        with ExitStack() as stack:
            for plugin in packages:
                stack.enter_context(plugin_withdrawn(plugin))
            # The url tag imports reverse inside render(), so patching the module attribute reaches it.
            stack.enter_context(mock.patch("django.urls.reverse", side_effect=reverse_without_plugin_routes))
            for name in PAGES:
                with self.subTest(page=name):
                    self.assertEqual(client.get(reverse(name)).status_code, 200)

    def test_the_withdrawal_reaches_the_routes_it_names(self) -> None:
        """Anti-vacuity: with no package plugin installed there is nothing to withdraw, and the test above proves nothing."""
        packages = [info.name for info in plugin_registry.plugins() if info.is_active and info.source.is_package]
        if not packages:
            self.skipTest("no package plugin is installed")
        self.assertTrue(
            {
                name
                for plugin in packages
                for pattern in contributed(plugin, points.ROOT_URLS)
                for name in _route_names(pattern)
            }
        )
