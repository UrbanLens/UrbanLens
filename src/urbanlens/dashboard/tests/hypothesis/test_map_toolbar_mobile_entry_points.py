"""Tests that every map panel has a toolbar entry point, not just an edge handle."""

from __future__ import annotations

from django.template.loader import render_to_string

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.templatetags.map_components import MAP_TOOL_REGISTRY, map_toolbar

# What the main map page renders - see pages/map/index.html.
_MAIN_MAP_TOOLS = "add_pin,import,search,pin_list,select,screenshot"


def _render(tools: str) -> str:
    return render_to_string("dashboard/partials/map/_map_toolbar.html", map_toolbar(tools=tools))


class PinListToolbarEntryTests(SimpleTestCase):
    def test_pin_list_tool_is_registered(self) -> None:
        self.assertIn("pin_list", MAP_TOOL_REGISTRY)

    def test_button_id_matches_the_id_the_toggle_looks_up(self) -> None:
        # _togglePinListPanel() resolves this exact id to sync the active state.
        self.assertEqual(MAP_TOOL_REGISTRY["pin_list"].button_id, "pin-list-button")

    def test_pin_list_button_renders_in_the_main_map_toolbar(self) -> None:
        html = _render(_MAIN_MAP_TOOLS)
        self.assertIn('id="pin-list-button"', html)
        self.assertIn('data-map-tool="toggle-pin-list"', html)

    def test_filter_panel_also_has_a_toolbar_entry(self) -> None:
        # The filter panel's edge handle is desktop-only for the same reason, so
        # the "search" tool is likewise the only way to open it on a phone.
        html = _render(_MAIN_MAP_TOOLS)
        self.assertIn('id="search-pins-button"', html)
        self.assertIn('data-map-tool="toggle-filter-panel"', html)

    def test_unknown_tool_keys_are_still_ignored(self) -> None:
        self.assertNotIn("pin-list-button", _render("add_pin,not_a_real_tool"))


class ToolbarCarriesNoScriptTests(SimpleTestCase):
    """Each button names its action for ``map-toolbar-actions.ts``; none carries a handler."""

    def test_no_tool_renders_an_inline_handler(self) -> None:
        html = _render(",".join(MAP_TOOL_REGISTRY))
        self.assertNotRegex(html, r"\son[a-z]+\s*=")
        self.assertIn('data-map-tool="screenshot"', html)

    def test_a_trips_screenshot_carries_the_trip_name_as_json(self) -> None:
        html = render_to_string(
            "dashboard/partials/map/_map_toolbar.html",
            map_toolbar(tools="screenshot", screenshot_trip_name='Ohio "<2026>"'),
        )
        self.assertIn(
            'data-screenshot-context="{&quot;tripName&quot;: &quot;Ohio \\&quot;&lt;2026&gt;\\&quot;&quot;}"', html
        )

    def test_the_pin_page_opens_its_own_screenshot_composer(self) -> None:
        html = render_to_string(
            "dashboard/partials/map/_map_toolbar.html",
            map_toolbar(tools="screenshot", screenshot_action="pin-screenshot"),
        )
        self.assertIn('data-map-tool="pin-screenshot"', html)
        self.assertNotIn("data-screenshot-context", html)
