"""A plugin named in UL_DISABLED_PLUGINS registers no hooks, so it contributes nothing to the install."""

from __future__ import annotations

from unittest import mock

from django.test import SimpleTestCase

from urbanlens.dashboard.plugins import HookRegistry, PluginRegistry, UrbanLensPlugin
from urbanlens.UrbanLens.settings.app import settings as app_settings


class SupporterPlugin(UrbanLensPlugin):
    name = "test-supporter"

    def register(self, hooks: HookRegistry) -> None:
        hooks.add_filter("active_supporter_count", lambda count, as_of=None: count + 1)


class DisabledPluginTests(SimpleTestCase):
    def _discover(self, disabled: list[str]) -> HookRegistry:
        hook_registry = HookRegistry()
        registry = PluginRegistry()
        with (
            mock.patch.object(app_settings, "disabled_plugins", disabled),
            mock.patch("urbanlens.dashboard.plugins.registry.hooks", hook_registry),
            mock.patch.object(registry, "_load_builtin", side_effect=lambda: registry.register(SupporterPlugin)),
            mock.patch.object(registry, "_load_settings_modules"),
            mock.patch.object(registry, "_load_entry_points"),
        ):
            registry.discover()
        return hook_registry

    def test_an_enabled_plugin_registers_its_hooks(self) -> None:
        self.assertEqual(self._discover([]).apply_filters("active_supporter_count", 0), 1)

    def test_a_disabled_plugin_registers_no_hooks(self) -> None:
        self.assertEqual(self._discover(["test-supporter"]).apply_filters("active_supporter_count", 0), 0)
