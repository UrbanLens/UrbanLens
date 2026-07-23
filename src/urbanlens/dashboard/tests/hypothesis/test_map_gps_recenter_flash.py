"""Regression coverage for UL-221: a returning GPS-mode user's map must not
visibly jump once a fresh geolocation fix resolves after loading at a
cached position.

This is a client-side JS bug (map/index.html's inline script), so it can't
be verified at runtime without a browser - what's tested here is that the
fix's code (the `_hadCachedLocation` guard around the live `map.setView`
call in the geolocation success callback) is actually present in the
rendered page, as a regression guard against an accidental revert. The
underlying behavior itself needs manual/browser verification.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import MapCenterMode


class GpsRecenterGuardRenderedTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.user.profile.map_center_mode = MapCenterMode.GPS
        self.user.profile.save(update_fields=["map_center_mode"])
        self.client.force_login(self.user)

    def test_geolocation_success_callback_guards_the_live_recenter(self) -> None:
        """Without this guard, every returning GPS-mode user's map loaded at
        the cached position and then silently jumped once a fresh fix
        arrived - contradicting the surrounding code's own documented
        intent ("the map won't visibly jump")."""
        body = self.client.get(reverse("map.view")).content.decode()
        self.assertIn("_hadCachedLocation", body)
        self.assertIn("if (!_hadCachedLocation) {", body)

    def test_had_cached_location_is_captured_before_the_async_geolocation_call(self) -> None:
        """The guard must snapshot _cachedUserLoc synchronously, before the
        geolocation callback runs - not re-read it inside the callback,
        where nothing has invalidated it but the intent would be unclear.
        The page has an unrelated, earlier getCurrentPosition call (the "My
        Location" search button), so the search for the recenter block's own
        call must start after the guard capture, not from the top of the page.
        """
        body = self.client.get(reverse("map.view")).content.decode()
        capture_index = body.find("const _hadCachedLocation = !!_cachedUserLoc;")
        self.assertNotEqual(capture_index, -1, "Guard variable not found in rendered page")
        callback_index = body.find("navigator.geolocation.getCurrentPosition(", capture_index)
        self.assertNotEqual(callback_index, -1, "geolocation call not found after the guard capture")
        self.assertLess(capture_index, callback_index, "Guard must be captured before the async geolocation call starts")
