"""The plugin framework: seams, the registry's lifecycle, discovery, and the bundled plugins' wiring."""

from __future__ import annotations

from typing import ClassVar
from unittest import mock

from django.template import Context, Template
from django.test import SimpleTestCase

from hypothesis import given, strategies as st
from urbanlens.dashboard.plugins import PluginContext, PluginRegistry, PluginState, UrbanLensPlugin, plugin_registry
from urbanlens.dashboard.plugins.extensions import (
    ActionHook,
    ExtensionPoint,
    FilterHook,
    Registry,
    TemplateSlot,
    catalog,
    discard_contributions,
)
from urbanlens.dashboard.plugins.loader import PluginSource, find_plugin_sources, plugin_apps
from urbanlens.dashboard.plugins.testing import contributed, plugin_active
from urbanlens.UrbanLens.settings.app import settings as app_settings

_names = iter(range(10**9))


def _unique(prefix: str) -> str:
    """Seam names are process-wide, so each test declares its own."""
    return f"test.{prefix}.{next(_names)}"


class ExtensionPointTests(SimpleTestCase):
    @given(st.lists(st.tuples(st.integers(-50, 50), st.text(max_size=5)), max_size=20))
    def test_values_come_back_in_priority_then_registration_order(self, entries: list[tuple[int, str]]) -> None:
        point: ExtensionPoint[str] = ExtensionPoint(_unique("order"))
        for priority, value in entries:
            point.add(value, priority=priority)
        expected = [
            value for _index, (_priority, value) in sorted(enumerate(entries), key=lambda item: (item[1][0], item[0]))
        ]
        self.assertEqual(point.items(), expected)

    def test_discard_withdraws_only_that_owner(self) -> None:
        point: ExtensionPoint[str] = ExtensionPoint(_unique("owners"))
        point.add("core")
        point.add("mine", owner="a")
        point.add("theirs", owner="b")
        self.assertEqual(point.discard(owner="a"), 1)
        self.assertEqual(point.items(), ["core", "theirs"])

    def test_overridden_restores_what_was_there(self) -> None:
        point: ExtensionPoint[str] = ExtensionPoint(_unique("override"))
        point.add("original", owner="a")
        with point.overridden(["stand-in"]):
            self.assertEqual(point.items(), ["stand-in"])
        self.assertEqual(point.items(), ["original"])

    def test_a_seam_name_is_declared_once(self) -> None:
        name = _unique("dup")
        ExtensionPoint(name)
        with self.assertRaises(ValueError):
            ExtensionPoint(name)


class RegistryTests(SimpleTestCase):
    def test_the_higher_priority_claim_on_a_key_wins(self) -> None:
        registry: Registry[str, int] = Registry(_unique("registry"))
        registry.add("key", 1, owner="late", priority=200)
        registry.add("key", 2, owner="early", priority=10)
        registry.add("other", 3)
        with self.assertLogs("urbanlens.dashboard.plugins.extensions", level="WARNING"):
            self.assertEqual(registry.as_dict(), {"key": 2, "other": 3})
        self.assertEqual(registry.get("key"), 2)
        self.assertIsNone(registry.get("missing"))


class HookTests(SimpleTestCase):
    def test_filters_chain_in_priority_order(self) -> None:
        hook: FilterHook[list[str], [str]] = FilterHook(_unique("filter"))
        hook.connect(lambda value, suffix: [*value, f"late{suffix}"], priority=20)
        hook.connect(lambda value, suffix: [*value, f"early{suffix}"], priority=5)
        self.assertEqual(hook.apply([], "!"), ["early!", "late!"])

    def test_a_failing_filter_is_skipped_and_the_value_passes_on(self) -> None:
        hook: FilterHook[int, []] = FilterHook(_unique("skip"))

        def broken(value: int) -> int:
            raise RuntimeError("boom")

        hook.connect(broken)
        hook.connect(lambda value: value + 1)
        with self.assertLogs("urbanlens.dashboard.plugins.extensions", level="ERROR"):
            self.assertEqual(hook.apply(1), 2)

    def test_a_fail_closed_filter_propagates(self) -> None:
        hook: FilterHook[int, []] = FilterHook(_unique("raise"), on_error="raise")

        def broken(value: int) -> int:
            raise RuntimeError("boom")

        hook.connect(broken)
        with self.assertRaises(RuntimeError):
            hook.apply(1)

    def test_actions_reach_every_callback_despite_a_failure(self) -> None:
        hook: ActionHook[[list[str]]] = ActionHook(_unique("action"))
        received: list[str] = []

        def broken(sink: list[str]) -> None:
            raise RuntimeError("boom")

        hook.connect(broken, priority=1)
        hook.connect(lambda sink: sink.append("ran"), priority=2)
        with self.assertLogs("urbanlens.dashboard.plugins.extensions", level="ERROR"):
            hook.fire(received)
        self.assertEqual(received, ["ran"])

    def test_disconnect_removes_a_callback(self) -> None:
        hook: FilterHook[int, []] = FilterHook(_unique("disconnect"))

        def double(value: int) -> int:
            return value * 2

        hook.connect(double)
        self.assertTrue(hook.disconnect(double))
        self.assertFalse(hook.disconnect(double))
        self.assertEqual(hook.apply(3), 3)


POINT: ExtensionPoint[str] = ExtensionPoint("test.registry.point")
FILTER: FilterHook[list[str], []] = FilterHook("test.registry.filter")


class _Contributing(UrbanLensPlugin):
    name: ClassVar[str] = "test_contributing"
    order: ClassVar[int] = 30

    def setup(self, plugin: PluginContext) -> None:
        plugin.add(POINT, "from-plugin")
        plugin.add(POINT, "first", priority=1)
        plugin.filter(FILTER, lambda value: [*value, "filtered"])


class _Broken(UrbanLensPlugin):
    name: ClassVar[str] = "test_broken"

    def setup(self, plugin: PluginContext) -> None:
        plugin.add(POINT, "half-done")
        raise RuntimeError("setup failed")


class _Dependent(UrbanLensPlugin):
    name: ClassVar[str] = "test_dependent"
    requires: ClassVar[tuple[str, ...]] = ("test_absent",)

    def setup(self, plugin: PluginContext) -> None:
        plugin.add(POINT, "should-not-appear")


class PluginLifecycleTests(SimpleTestCase):
    def tearDown(self) -> None:
        for name in ("test_contributing", "test_broken", "test_dependent"):
            plugin_registry.unregister(name)

    def test_contributions_take_the_plugin_order_unless_given_a_priority(self) -> None:
        with plugin_active(_Contributing):
            self.assertEqual(POINT.items(), ["first", "from-plugin"])
            self.assertEqual([c.priority for c in POINT.contributions()], [1, 30])
            self.assertEqual(FILTER.apply([]), ["filtered"])
        self.assertEqual(POINT.items(), [])
        self.assertEqual(FILTER.apply([]), [])

    def test_a_plugin_whose_setup_fails_is_inactive_and_withdrawn(self) -> None:
        with self.assertLogs("urbanlens.dashboard.plugins.registry", level="ERROR"):
            info = plugin_registry.register(_Broken)
        assert info is not None
        self.assertIs(info.state, PluginState.FAILED)
        self.assertEqual(contributed("test_broken", POINT), [])

    def test_a_disabled_plugin_contributes_nothing(self) -> None:
        registry = PluginRegistry()
        source = PluginSource(name="test_contributing", module=__name__, origin="settings", is_package=False)
        with (
            mock.patch("urbanlens.dashboard.plugins.registry.find_plugin_sources", return_value=[source]),
            mock.patch.object(app_settings, "disabled_plugins", ["test_contributing"]),
            mock.patch.object(app_settings, "plugin_modules", []),
        ):
            registry.discover(force=True)
        try:
            info = registry.info("test_contributing")
            assert info is not None
            self.assertIs(info.state, PluginState.DISABLED)
            self.assertEqual(contributed("test_contributing", POINT), [])
        finally:
            registry._reset()

    def test_a_plugin_missing_a_requirement_stays_inactive(self) -> None:
        registry = PluginRegistry()
        source = PluginSource(name="test_dependent", module=__name__, origin="settings", is_package=False)
        with (
            mock.patch("urbanlens.dashboard.plugins.registry.find_plugin_sources", return_value=[source]),
            mock.patch.object(app_settings, "disabled_plugins", []),
        ):
            registry.discover(force=True)
        try:
            info = registry.info("test_dependent")
            assert info is not None
            self.assertIs(info.state, PluginState.MISSING_REQUIREMENT)
            self.assertNotIn("should-not-appear", POINT.items())
        finally:
            registry._reset()

    def test_a_duplicate_name_keeps_the_first(self) -> None:
        with plugin_active(_Contributing), self.assertLogs("urbanlens.dashboard.plugins.registry", level="WARNING"):
            self.assertIsNone(plugin_registry.register(_Contributing))


class LoaderTests(SimpleTestCase):
    def test_bundled_packages_and_builtin_modules_are_found_without_importing_them(self) -> None:
        sources = {source.name: source for source in find_plugin_sources()}
        self.assertIn("nominatim", sources)
        self.assertFalse(sources["nominatim"].is_package)
        for source in sources.values():
            if source.origin == "bundled":
                self.assertTrue(source.is_package)
                self.assertEqual(source.plugin_module, f"{source.module}.plugin")

    def test_only_enabled_packages_become_apps(self) -> None:
        sources = [
            PluginSource(name="a", module="pkg.a", origin="bundled", is_package=True),
            PluginSource(name="b", module="pkg.b", origin="bundled", is_package=True),
            PluginSource(name="c", module="mod.c", origin="builtin", is_package=False),
        ]
        self.assertEqual(plugin_apps(sources, ["b"]), ["pkg.a"])

    def test_an_unfindable_settings_module_is_logged_and_skipped(self) -> None:
        with self.assertLogs("urbanlens.dashboard.plugins.loader", level="ERROR"):
            sources = find_plugin_sources(["urbanlens.no_such_plugin_module"])
        self.assertNotIn("no_such_plugin_module", {source.name for source in sources})

    def test_every_installed_package_plugin_is_an_app(self) -> None:
        from django.apps import apps

        for info in plugin_registry.plugins():
            if info.source.is_package and info.state is not PluginState.DISABLED:
                self.assertTrue(apps.is_installed(info.source.module), info.name)


class TemplateSlotTests(SimpleTestCase):
    def test_a_slot_renders_its_contributions_in_order(self) -> None:
        slot = TemplateSlot(_unique("slot"))
        template = Template('{% plugin_slot "' + slot.name + '" %}')
        self.assertEqual(template.render(Context({})), "")
        slot.add("second.html", priority=20)
        slot.add("first.html", priority=10)
        with mock.patch(
            "urbanlens.dashboard.templatetags.plugin_slots.render_to_string",
            side_effect=lambda name, *_a, **_k: f"<{name}>",
        ):
            self.assertEqual(template.render(Context({})), "<first.html><second.html>")

    def test_an_undeclared_slot_fails_loudly(self) -> None:
        from urbanlens.dashboard.templatetags.plugin_slots import UnknownSlotError

        with self.assertRaises(UnknownSlotError):
            Template('{% plugin_slot "no.such.slot" %}').render(Context({}))

    def test_every_slot_core_templates_name_is_declared(self) -> None:
        import pathlib
        import re

        templates = pathlib.Path(__file__).resolve().parents[2] / "templates"
        declared = {name for name, seam in catalog().items() if isinstance(seam, TemplateSlot)}
        for path in templates.rglob("*.html"):
            for name in re.findall(r'plugin_slot "([^"]+)"', path.read_text(encoding="utf-8")):
                self.assertIn(name, declared, f"{path.name} names undeclared slot {name!r}")


class BundledPluginWiringTests(SimpleTestCase):
    """The bundled plugins are found and wired into their consumers."""

    def test_bundled_plugins_are_active(self) -> None:
        names = {plugin.name for plugin in plugin_registry.active_plugins()}
        expected = {"wikipedia", "nominatim", "usgs", "wikimedia", "google_maps", "google_places", "esri"}
        self.assertTrue(expected <= names, f"missing: {expected - names}")

    def test_all_service_defaults_merges_static_and_plugin_entries(self) -> None:
        from urbanlens.dashboard.services.core.rate_limiter import all_service_defaults

        merged = all_service_defaults()
        self.assertIn("wikipedia", merged)
        self.assertIn("google_places", merged)
        self.assertIn("openweathermap", merged)

    def test_name_providers_cover_the_naming_sources(self) -> None:
        from urbanlens.dashboard.plugins.points import NAME_PROVIDERS

        sources = [provider.source for provider in NAME_PROVIDERS]
        for expected in ("google_places", "wikipedia"):
            self.assertIn(expected, sources)

    def test_panel_sources_contain_core_and_plugin_panels(self) -> None:
        from urbanlens.dashboard.services.pins.external_data import panel_sources

        sources = panel_sources()
        for key in ("boundary", "satellite", "street_view", "wikipedia", "nominatim", "usgs_topo", "wikimedia"):
            self.assertIn(key, sources)

    def test_public_imagery_providers_lead_in_display_order(self) -> None:
        from urbanlens.dashboard.plugins.points import SATELLITE_PROVIDERS, STREET_VIEW_PROVIDERS

        satellite = [factory().__class__.__name__ for factory in SATELLITE_PROVIDERS]
        self.assertEqual(satellite[:2], ["GoogleMapsGateway", "EsriGateway"])
        street = [factory().__class__.__name__ for factory in STREET_VIEW_PROVIDERS]
        self.assertEqual(street[:1], ["GoogleMapsGateway"])

    def test_unknown_panel_source_is_rejected_cleanly(self) -> None:
        from urbanlens.dashboard.services.pins.external_data import get_panel_source, schedule_panel_fetch

        self.assertIsNone(get_panel_source("no_such_panel"))
        self.assertFalse(schedule_panel_fetch("no_such_panel", pin=None))


def test_discard_contributions_of_an_unknown_owner_is_a_no_op() -> None:
    assert discard_contributions(owner="no-such-plugin") == 0
