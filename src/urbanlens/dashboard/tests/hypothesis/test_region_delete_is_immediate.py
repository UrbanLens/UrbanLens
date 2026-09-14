"""A deleted drawn region must stay deleted (P27).

leaflet-draw's remove tool only stages a deletion, committed through its own Save action; starting
any other draw or edit tool reverts it. Region maps use `shared/region-delete.ts` instead, which
commits each deletion when it is clicked. These read the source; the behaviour was verified in a browser.
"""

from __future__ import annotations

from pathlib import Path
import re

from urbanlens.core.tests.testcase import SimpleTestCase

_DASHBOARD = Path(__file__).resolve().parents[2]
_EDIT_OPTIONS = re.compile(r"\bedit\s*:\s*\{")
_REMOVE_DISABLED = re.compile(r"\bremove\s*:\s*false\b")


def _enables_remove_tool(source: str) -> bool:
    """leaflet-draw enables the remove tool unless the edit options say ``remove: false``."""
    if "Control.Draw(" not in source:
        return False
    for match in _EDIT_OPTIONS.finditer(source):
        depth, end = 1, match.end()
        while depth and end < len(source):
            depth += {"{": 1, "}": -1}.get(source[end], 0)
            end += 1
        if not _REMOVE_DISABLED.search(source[match.end() : end]):
            return True
    return False


_REGION_MAPS = (
    "templates/dashboard/partials/pin_lists/_saved_filter_dialog_scripts.html",
    "templates/dashboard/pages/pin_lists/detail.html",
)


class RegionDeletionIsImmediateTests(SimpleTestCase):
    def test_no_draw_control_enables_the_staged_remove_tool(self) -> None:
        offenders = [
            str(path.relative_to(_DASHBOARD))
            for root in ("templates", "frontend/ts")
            for path in (_DASHBOARD / root).rglob("*")
            if path.suffix in {".html", ".ts"}
            and not path.name.endswith(".test.ts")
            and _enables_remove_tool(path.read_text(encoding="utf-8"))
        ]

        self.assertEqual(offenders, [])

    def test_every_region_map_adds_the_immediate_delete_tool(self) -> None:
        for name in _REGION_MAPS:
            with self.subTest(template=name):
                self.assertIn("window.RegionDelete.add(", (_DASHBOARD / name).read_text(encoding="utf-8"))

    def test_every_region_map_loads_each_stored_part_as_its_own_layer(self) -> None:
        """A stored MultiPolygon loaded whole is one layer, which delete and edit act on as a whole (P120)."""
        for name in _REGION_MAPS:
            with self.subTest(template=name):
                self.assertIn("window.RegionDelete.polygonParts(", (_DASHBOARD / name).read_text(encoding="utf-8"))
