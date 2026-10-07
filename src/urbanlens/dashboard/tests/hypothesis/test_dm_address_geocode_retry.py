"""A DM address that could not be geocoded because the network failed is retried by its task, not recorded as "no match"."""

from __future__ import annotations

from unittest import mock

from django.test import SimpleTestCase
import requests

from urbanlens.dashboard import tasks
from urbanlens.dashboard.services.core.gateway import GatewayRequestError
from urbanlens.dashboard.services.messaging import dm_location_detection
from urbanlens.UrbanLens.settings.app import settings as app_settings

_GEOCODE = "urbanlens.dashboard.services.apis.locations.google.geocoding.GoogleGeocodingGateway.geocode_place_name"
_STREET_RESULT = {"results": [{"types": ["street_address"], "geometry": {"location": {"lat": 42.5, "lng": -73.5}}}]}


class GeocodeFailureTests(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(app_settings, "google_unrestricted_api_key", "test-key")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_transport_failure_reaches_the_task_so_it_can_retry(self) -> None:
        for failure in (
            requests.ConnectionError("down"),
            requests.Timeout("slow"),
            requests.HTTPError("503"),
            OSError("reset"),
        ):
            with (
                mock.patch(_GEOCODE, side_effect=failure),
                self.subTest(failure=type(failure).__name__),
                self.assertRaises(OSError),
            ):
                dm_location_detection._geocode_address("12 Mill Street")

    def test_every_failure_the_service_lets_out_is_one_the_task_retries(self) -> None:
        self.assertTrue(issubclass(requests.RequestException, tuple(tasks.detect_dm_address_mentions.autoretry_for)))

    def test_a_refusal_or_unparseable_answer_is_no_match(self) -> None:
        for failure in (GatewayRequestError("refused"), ValueError("bad json"), KeyError("results"), TypeError("x")):
            with mock.patch(_GEOCODE, side_effect=failure), self.subTest(failure=type(failure).__name__):
                self.assertIsNone(dm_location_detection._geocode_address("12 Mill Street"))

    def test_a_street_level_answer_is_still_returned(self) -> None:
        with mock.patch(_GEOCODE, return_value=_STREET_RESULT):
            self.assertEqual(dm_location_detection._geocode_address("12 Mill Street"), (42.5, -73.5))

    def test_the_failure_log_does_not_carry_the_address(self) -> None:
        with (
            mock.patch(_GEOCODE, side_effect=GatewayRequestError("refused")),
            self.assertLogs(dm_location_detection.logger, "WARNING") as logs,
        ):
            dm_location_detection._geocode_address("12 Mill Street")
        self.assertNotIn("Mill Street", "\n".join(logs.output))
