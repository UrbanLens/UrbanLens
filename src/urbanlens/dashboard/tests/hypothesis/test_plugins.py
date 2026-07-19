"""Tests for the plugin framework: hook bus, plugin registry, and integrations.

- HookRegistry: pure in-memory; priority ordering property-tested with
  hypothesis.
- PluginRegistry: exercised with locally defined dummy plugins on a fresh
  registry instance (never the app-wide singleton, which real discovery owns).
- Integration: the builtin plugins really are discovered, feed the rate
  limiter's merged defaults, and populate external_data's panel registry.
  DB-free throughout.
"""

from __future__ import annotations

from typing import ClassVar
from unittest.mock import patch

from hypothesis import given, strategies as st

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.plugins import UrbanLensPlugin, plugin_registry
from urbanlens.dashboard.plugins.hooks import HookRegistry
from urbanlens.dashboard.plugins.registry import PluginRegistry
from urbanlens.dashboard.services.locations.name_resolution import NameProvider
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults

# -- HookRegistry ----------------------------------------------------------------


class HookRegistryFilterTests(SimpleTestCase):
    """apply_filters chains callbacks in priority order and survives failures."""

    def test_filters_apply_in_priority_order(self) -> None:
        hooks = HookRegistry()
        hooks.add_filter("value", lambda v: v + "b", priority=20)
        hooks.add_filter("value", lambda v: v + "a", priority=10)
        self.assertEqual(hooks.apply_filters("value", ""), "ab")

    def test_equal_priority_preserves_registration_order(self) -> None:
        hooks = HookRegistry()
        hooks.add_filter("value", lambda v: v + "first")
        hooks.add_filter("value", lambda v: v + "-second")
        self.assertEqual(hooks.apply_filters("value", ""), "first-second")

    def test_unknown_filter_returns_value_unchanged(self) -> None:
        self.assertEqual(HookRegistry().apply_filters("nope", 42), 42)

    def test_broken_callback_is_skipped(self) -> None:
        hooks = HookRegistry()

        def boom(_value):
            raise RuntimeError("broken plugin")

        hooks.add_filter("value", boom, priority=5)
        hooks.add_filter("value", lambda v: v + 1, priority=10)
        with self.assertLogs("urbanlens.dashboard.plugins.hooks", level="ERROR"):
            self.assertEqual(hooks.apply_filters("value", 1), 2)

    def test_remove_filter(self) -> None:
        hooks = HookRegistry()

        def add_one(v):
            return v + 1

        hooks.add_filter("value", add_one)
        self.assertTrue(hooks.remove_filter("value", add_one))
        self.assertFalse(hooks.remove_filter("value", add_one))
        self.assertEqual(hooks.apply_filters("value", 1), 1)

    def test_extra_arguments_are_forwarded(self) -> None:
        hooks = HookRegistry()
        hooks.add_filter("value", lambda v, pin: v + [pin])
        self.assertEqual(hooks.apply_filters("value", [], "pin-7"), ["pin-7"])

    @given(priorities=st.lists(st.integers(min_value=-100, max_value=100), min_size=1, max_size=8))
    def test_callbacks_always_run_in_ascending_priority(self, priorities: list[int]) -> None:
        hooks = HookRegistry()
        for priority in priorities:
            hooks.add_filter("order", lambda seen, p=priority: seen + [p], priority=priority)
        seen = hooks.apply_filters("order", [])
        self.assertEqual(seen, sorted(priorities))


class HookRegistryActionTests(SimpleTestCase):
    """do_action notifies every callback and isolates failures."""

    def test_actions_run_with_arguments(self) -> None:
        hooks = HookRegistry()
        calls: list[tuple] = []
        hooks.add_action("ping", lambda *args, **kwargs: calls.append((args, kwargs)))
        hooks.do_action("ping", 1, flag=True)
        self.assertEqual(calls, [((1,), {"flag": True})])

    def test_broken_action_does_not_stop_others(self) -> None:
        hooks = HookRegistry()
        calls: list[str] = []

        def boom() -> None:
            raise RuntimeError("broken plugin")

        hooks.add_action("ping", boom, priority=5)
        hooks.add_action("ping", lambda: calls.append("ran"), priority=10)
        with self.assertLogs("urbanlens.dashboard.plugins.hooks", level="ERROR"):
            hooks.do_action("ping")
        self.assertEqual(calls, ["ran"])


# -- PluginRegistry ----------------------------------------------------------------


class AlphaPlugin(UrbanLensPlugin):
    """Dummy plugin contributing service defaults."""

    name: ClassVar[str] = "alpha"
    order: ClassVar[int] = 10

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        return {"alpha_api": ServiceDefaults(display_name="Alpha API")}


class BetaPlugin(UrbanLensPlugin):
    """Dummy plugin, ordered after alpha."""

    name: ClassVar[str] = "beta"
    order: ClassVar[int] = 20

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        return {"beta_api": ServiceDefaults(display_name="Beta API")}


class PluginRegistryTests(SimpleTestCase):
    """Registration, ordering, duplicates, and enabled-state filtering."""

    def _registry(self, *plugins: type[UrbanLensPlugin]) -> PluginRegistry:
        registry = PluginRegistry()
        for plugin in plugins:
            registry.register(plugin)
        return registry

    def test_plugins_sorted_by_order_then_name(self) -> None:
        registry = self._registry(BetaPlugin, AlphaPlugin)
        self.assertEqual([info.plugin.name for info in registry.plugins()], ["alpha", "beta"])

    def test_duplicate_name_keeps_first(self) -> None:
        registry = PluginRegistry()
        first = registry.register(AlphaPlugin)
        with self.assertLogs("urbanlens.dashboard.plugins.registry", level="WARNING"):
            self.assertIsNone(registry.register(AlphaPlugin))
        self.assertIs(registry.get("alpha"), first)

    def test_plugin_without_name_is_rejected(self) -> None:
        class Nameless(UrbanLensPlugin):
            pass

        registry = PluginRegistry()
        with self.assertLogs("urbanlens.dashboard.plugins.registry", level="WARNING"):
            self.assertIsNone(registry.register(Nameless))
        self.assertEqual(registry.plugins(), [])

    def test_service_defaults_merge_across_plugins(self) -> None:
        registry = self._registry(AlphaPlugin, BetaPlugin)
        defaults = registry.service_defaults()
        self.assertEqual(set(defaults), {"alpha_api", "beta_api"})

    def test_disabled_plugin_contributes_nothing(self) -> None:
        registry = self._registry(AlphaPlugin, BetaPlugin)
        with patch.object(PluginRegistry, "is_enabled", side_effect=lambda name: name != "alpha"):
            defaults = registry.service_defaults()
            self.assertEqual(set(defaults), {"beta_api"})
            self.assertEqual([p.name for p in registry.enabled_plugins()], ["beta"])
        # All plugins remain visible for the admin UI regardless of state.
        self.assertEqual(len(registry.plugins()), 2)

    def test_broken_contribution_is_isolated(self) -> None:
        class Broken(UrbanLensPlugin):
            name: ClassVar[str] = "broken"

            def get_panel_sources(self):
                raise RuntimeError("broken plugin")

        registry = self._registry(Broken)
        with self.assertLogs("urbanlens.dashboard.plugins.registry", level="ERROR"):
            self.assertEqual(registry.panel_sources(), [])

    def test_name_providers_aggregate_in_plugin_order(self) -> None:
        class EarlyNames(UrbanLensPlugin):
            name: ClassVar[str] = "early"
            order: ClassVar[int] = 5

            def get_name_providers(self) -> list[NameProvider]:
                return [NameProvider(source="early_source")]

        class LateNames(UrbanLensPlugin):
            name: ClassVar[str] = "late"
            order: ClassVar[int] = 50

            def get_name_providers(self) -> list[NameProvider]:
                return [NameProvider(source="late_source")]

        registry = self._registry(LateNames, EarlyNames)
        self.assertEqual([provider.source for provider in registry.name_providers()], ["early_source", "late_source"])


# -- Integration with real discovery ----------------------------------------------


class BuiltinDiscoveryTests(SimpleTestCase):
    """The bundled plugins are discovered and wired into their consumers."""

    def test_builtin_plugins_discovered(self) -> None:
        names = {info.plugin.name for info in plugin_registry.plugins()}
        expected = {
            "wikipedia",
            "nominatim",
            "usgs",
            "nps",
            "loopnet",
            "smithsonian",
            "wikimedia",
            "library_of_congress",
            "google_maps",
            "google_places",
            "esri",
            "nasa_gibs",
            "mapbox",
            "bing_maps",
            "open_aerial_map",
            "mapillary",
            "kartaview",
        }
        self.assertTrue(expected.issubset(names), f"missing: {expected - names}")

    def test_all_service_defaults_merges_static_and_plugin_entries(self) -> None:
        from urbanlens.dashboard.services.rate_limiter import all_service_defaults

        merged = all_service_defaults()
        # Plugin-declared services (google_places moved out of SERVICE_REGISTRY).
        self.assertIn("wikipedia", merged)
        self.assertIn("google_places", merged)
        # Static SERVICE_REGISTRY fallback for a not-yet-converted service.
        self.assertIn("openweathermap", merged)

    def test_builtin_name_providers_cover_the_naming_sources(self) -> None:
        sources = [provider.source for provider in plugin_registry.name_providers()]
        for expected in ("google_places", "wikipedia", "nps"):
            self.assertIn(expected, sources, f"name provider '{expected}' missing")

    def test_panel_sources_contains_core_and_plugin_panels(self) -> None:
        from urbanlens.dashboard.services.external_data import panel_sources

        sources = panel_sources()
        for key in ("boundary", "satellite", "street_view"):
            self.assertIn(key, sources, f"core panel '{key}' missing")
        for key in ("wikipedia", "nominatim", "usgs_topo", "nps", "loopnet", "smithsonian", "wikimedia", "loc"):
            self.assertIn(key, sources, f"plugin panel '{key}' missing")

    def test_imagery_chains_preserve_display_order(self) -> None:
        satellite = [type(p).__name__ for p in plugin_registry.satellite_providers()]
        self.assertEqual(
            satellite,
            [
                "GoogleMapsGateway",
                "AzureMapsRenderGateway",
                "EsriGateway",
                "NasaGibsGateway",
                "MapboxGateway",
                "BingMapsGateway",
                "OpenAerialMapGateway",
                "OpenTopoMapGateway",
            ],
        )
        street = [type(p).__name__ for p in plugin_registry.street_view_providers()]
        self.assertEqual(street, ["GoogleMapsGateway", "MapillaryGateway", "KartaViewGateway", "PanoramaxGateway"])

    def test_unknown_panel_source_is_rejected_cleanly(self) -> None:
        from urbanlens.dashboard.services.external_data import get_panel_source, schedule_panel_fetch

        self.assertIsNone(get_panel_source("no_such_panel"))
        self.assertFalse(schedule_panel_fetch("no_such_panel", pin=None))
