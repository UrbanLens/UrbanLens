"""A Google place-name lookup that couldn't reach Google stores nothing; one Google answered stores its answer (P214)."""

from __future__ import annotations

from unittest import mock

from django.core.cache import caches

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.api_call_log import ApiCallLog
from urbanlens.dashboard.models.google_place.model import GooglePlace
from urbanlens.dashboard.services.apis.locations.google.place_info import NO_INFORMATION, GooglePlaceService
from urbanlens.dashboard.services.core.outages import outages_observed, record_unanswered
from urbanlens.dashboard.tests.hypothesis.test_outage_not_cached_registry import _Upstream
from urbanlens.UrbanLens.settings.app import settings as app_settings


class OutagesObservedTests(SimpleTestCase):
    def test_a_block_sees_only_its_own_calls(self) -> None:
        record_unanswered("before")
        with outages_observed() as outer:
            record_unanswered("google_geocoding")
            with outages_observed() as inner:
                record_unanswered("google_places")
        record_unanswered("after")

        self.assertEqual(outer.services, ["google_geocoding", "google_places"])
        self.assertEqual(inner.services, ["google_places"])

    def test_an_empty_block_is_falsy(self) -> None:
        with outages_observed() as log:
            pass

        self.assertFalse(log)


class GooglePlaceNameTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(app_settings, "google_unrestricted_api_key", "test-key")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _place(self) -> GooglePlace:
        for alias in caches:
            caches[alias].clear()
        ApiCallLog.objects.all().delete()
        GooglePlace.objects.all().delete()
        return GooglePlace.objects.create(latitude="41.7321", longitude="-73.9262")

    def test_a_lookup_that_reached_nothing_is_not_stored(self) -> None:
        for mode in ("refused", "timeout", "503"):
            with self.subTest(mode=mode):
                place = self._place()
                with _Upstream(mode).cut() as upstream:
                    shown = GooglePlaceService().resolve_place_name(place)

                place.refresh_from_db()
                self.assertGreater(upstream.attempts, 0)
                self.assertEqual(shown, NO_INFORMATION)
                self.assertFalse(place.cached_place_name)

    def test_an_answer_with_no_name_is_stored(self) -> None:
        place = self._place()
        with mock.patch.object(GooglePlaceService, "_resolve_name", return_value=None):
            GooglePlaceService().resolve_place_name(place)

        place.refresh_from_db()
        self.assertEqual(place.cached_place_name, NO_INFORMATION)

    def test_a_found_name_is_stored(self) -> None:
        place = self._place()
        with mock.patch.object(GooglePlaceService, "_resolve_name", return_value="Hudson River State Hospital"):
            GooglePlaceService().resolve_place_name(place)

        place.refresh_from_db()
        self.assertEqual(place.cached_place_name, "Hudson River State Hospital")
