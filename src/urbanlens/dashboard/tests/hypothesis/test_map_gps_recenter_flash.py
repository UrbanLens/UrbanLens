"""Regression coverage for UL-221: a returning GPS-mode user's map must not visibly jump once a fresh geolocation fix resolves after loading at a cached position."""

from __future__ import annotations

from pathlib import Path

from urbanlens.core.tests.testcase import SimpleTestCase

#: P92 moved this from an inline <script> into a bundled TS entry (P124); the guard is
#: static code, unconditional on any given viewer, so it is checked against the source
#: file directly rather than a per-request response body.
_SCRIPT_SOURCE = (Path(__file__).resolve().parents[3] / "dashboard/frontend/ts/entries/map-page.ts").read_text()


class GpsRecenterGuardRenderedTests(SimpleTestCase):
    def test_geolocation_success_callback_guards_the_live_recenter(self) -> None:
        """Without this guard, every returning GPS-mode user's map loaded at the cached position and then silently jumped once a fresh fix arrived - contradicting the surrounding code's own documented intent ("the map won't visibly jump")."""
        self.assertIn("_hadCachedLocation", _SCRIPT_SOURCE)
        self.assertIn("if (!_hadCachedLocation) {", _SCRIPT_SOURCE)

    def test_had_cached_location_is_captured_before_the_async_geolocation_call(self) -> None:
        """The guard must snapshot _cachedUserLoc synchronously, before the geolocation callback runs - not re-read it inside the callback, where nothing has invalidated it but the intent would be unclear. The page has an unrelated, earlier getCurrentPosition call (the "My Location" search button), so the search for the recenter block's own call must start after the guard capture, not from the top of the page."""
        capture_index = _SCRIPT_SOURCE.find("const _hadCachedLocation = !!_cachedUserLoc;")
        self.assertNotEqual(capture_index, -1, "Guard variable not found in script source")
        callback_index = _SCRIPT_SOURCE.find("navigator.geolocation.getCurrentPosition(", capture_index)
        self.assertNotEqual(callback_index, -1, "geolocation call not found after the guard capture")
        self.assertLess(
            capture_index, callback_index, "Guard must be captured before the async geolocation call starts"
        )
