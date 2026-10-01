"""A slow panel fetch must not clear the next one's single-flight marker."""

from __future__ import annotations

from unittest import mock

from django.core.cache import cache
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.core.locks import acquire_lock
from urbanlens.dashboard.services.pins.external_data import get_panel_source, run_panel_fetch

_SOURCE_KEY = "photon"


class PanelFlightTokenTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = Profile.objects.get(user=baker.make("auth.User"))
        self.pin = baker.make(Pin, profile=self.profile, location=baker.make(Location))
        self.source = get_panel_source(_SOURCE_KEY)
        self.key = self.source.flight_key(self.pin)
        cache.delete(self.key)
        self.addCleanup(cache.delete, self.key)

    def _run(self, token):
        with mock.patch.object(type(self.source), "fetch", return_value=None):
            run_panel_fetch(_SOURCE_KEY, self.pin, token)

    def test_a_fetch_clears_its_own_marker(self) -> None:
        """Anchors the rest: the normal path must still release."""
        token = acquire_lock(self.key, 150)

        self._run(token)

        self.assertIsNotNone(acquire_lock(self.key, 150), "the fetch did not release its own marker")

    def test_a_fetch_that_outlived_its_marker_releases_nothing(self) -> None:
        """The bug: A's marker lapses, B claims one, A's finally deletes B's."""
        stale_token = acquire_lock(self.key, 150)
        cache.delete(self.key)  # A's marker lapses while A is still running
        successor = acquire_lock(self.key, 150)  # the next poll schedules B
        self.assertIsNotNone(successor)

        self._run(stale_token)

        self.assertIsNone(
            acquire_lock(self.key, 150),
            "the slow fetch cleared the next fetch's marker, so a third fetch could be dispatched",
        )

    def test_a_legacy_task_without_a_token_still_clears_the_marker(self) -> None:
        """Tasks queued before the token existed must not leak their marker."""
        acquire_lock(self.key, 150)

        self._run(None)

        self.assertIsNotNone(acquire_lock(self.key, 150), "a pre-token task left its marker behind")

    def test_the_marker_is_released_even_when_the_fetch_raises(self) -> None:
        token = acquire_lock(self.key, 150)

        with mock.patch.object(type(self.source), "fetch", side_effect=RuntimeError("upstream down")):
            run_panel_fetch(_SOURCE_KEY, self.pin, token)

        self.assertIsNotNone(acquire_lock(self.key, 150), "a failed fetch stranded its marker")
