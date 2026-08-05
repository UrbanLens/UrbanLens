"""Tests for the iNaturalist panel's render_context(), fetch(), and gate().

Regression coverage for linking to specific observations/area instead of
iNaturalist's homepage -. Now sourced through REData's
``/nature-observations/`` endpoint instead of a direct iNaturalist call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.plugins.builtin.inaturalist import INaturalistPanelSource
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin


class INaturalistPanelSourceTests(TestCase):
    """render_context() for the nearby-observations panel."""

    def setUp(self) -> None:
        super().setUp()
        self.source = INaturalistPanelSource()
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile)

    def test_no_observations_yields_none(self) -> None:
        self.assertIsNone(self.source.render_context(self.pin, {"observations": []}))

    def test_meta_entry_links_to_the_specific_observation(self) -> None:
        data = {
            "observations": [
                {"common_name": "Red Fox", "scientific_name": "Vulpes vulpes", "observed_on": "2025-05-01", "uri": "https://www.inaturalist.org/observations/12345"},
            ],
        }
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertEqual(ctx["meta"][0]["href"], "https://www.inaturalist.org/observations/12345")

    def test_observation_with_no_uri_has_no_href(self) -> None:
        data = {"observations": [{"common_name": "Red Fox", "scientific_name": "", "observed_on": "2025-05-01", "uri": ""}]}
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertEqual(ctx["meta"][0]["href"], "")

    def test_footer_link_is_scoped_to_the_pins_coordinates(self) -> None:
        # Location rows are coordinate-immutable (see LocationCoordinateImmutability
        # tests) - build a pin against a Location created with the coordinates
        # already set, rather than mutating an existing one.
        location = baker.make("dashboard.Location", latitude=40.5, longitude=-74.5)
        pin: Pin = baker.make_recipe("dashboard.pin", profile=self.pin.profile, location=location)

        data = {"observations": [{"common_name": "Red Fox", "scientific_name": "", "observed_on": "2025-05-01", "uri": "https://x"}]}
        ctx = self.source.render_context(pin, data)

        assert ctx is not None
        url = ctx["footer_link"]["url"]
        self.assertIn("lat=40.5", url)
        self.assertIn("lng=-74.5", url)
        self.assertNotEqual(url, "https://www.inaturalist.org/observations")

    def test_obscured_observation_notes_the_location_is_approximate(self) -> None:
        """``attributes.obscured`` is load-bearing - an obscured sighting must not read as a precise one."""
        data = {
            "observations": [
                {
                    "common_name": "Spotted Turtle",
                    "scientific_name": "Clemmys guttata",
                    "observed_on": "2025-05-01",
                    "uri": "https://www.inaturalist.org/observations/12345",
                    "coordinate_uncertainty_meters": 27000,
                    "attributes": {"obscured": True},
                },
            ],
        }
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertIn("approximate location", ctx["meta"][0]["value"])

    def test_non_obscured_observation_has_no_approximate_location_note(self) -> None:
        data = {"observations": [{"common_name": "Red Fox", "scientific_name": "", "observed_on": "2025-05-01", "uri": "", "attributes": {"obscured": False}}]}
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertNotIn("approximate location", ctx["meta"][0]["value"])


class INaturalistPanelSourceFetchTests(TestCase):
    """fetch() persists REData's nature-observations results verbatim."""

    def setUp(self) -> None:
        super().setUp()
        self.source = INaturalistPanelSource()
        location = baker.make("dashboard.Location", latitude=40.5, longitude=-74.5)
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile, location=location)

    def test_fetch_caches_the_envelopes_results(self) -> None:
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        envelope = LocationContextEnvelope(
            count=1,
            complete=True,
            results=[{"provider": "inaturalist", "common_name": "Red Fox", "scientific_name": "Vulpes vulpes", "observed_on": "2025-05-01", "uri": "https://x"}],
        )
        with mock.patch("urbanlens.dashboard.services.apis.locations.redata_nature_gateway.RedataNatureObservationsGateway") as mock_gateway_cls:
            mock_gateway_cls.return_value.get_nearby_observations.return_value = envelope
            self.source.fetch(self.pin)

        cached = LocationCache.get_fresh(self.pin.location, "inaturalist")
        assert cached is not None
        self.assertEqual(cached.data["observations"], envelope.results)
        mock_gateway_cls.return_value.get_nearby_observations.assert_called_once_with(40.5, -74.5, radius_meters=2000, limit=10)

    def test_fetch_caches_an_explicit_empty_result(self) -> None:
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        envelope = LocationContextEnvelope(count=0, complete=True, results=[])
        with mock.patch("urbanlens.dashboard.services.apis.locations.redata_nature_gateway.RedataNatureObservationsGateway") as mock_gateway_cls:
            mock_gateway_cls.return_value.get_nearby_observations.return_value = envelope
            self.source.fetch(self.pin)

        cached = LocationCache.get_fresh(self.pin.location, "inaturalist")
        assert cached is not None
        self.assertEqual(cached.data["observations"], [])


class INaturalistPanelSourceGateTests(TestCase):
    """gate() also requires REData to be configured, since this panel has no other data source."""

    def setUp(self) -> None:
        super().setUp()
        self.source = INaturalistPanelSource()
        location = baker.make("dashboard.Location", latitude=40.5, longitude=-74.5)
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile, location=location)

    def test_gate_is_false_when_redata_is_not_configured(self) -> None:
        with mock.patch("urbanlens.dashboard.plugins.builtin.inaturalist.redata_configured", return_value=False):
            self.assertFalse(self.source.gate(self.pin))

    def test_gate_is_true_when_redata_is_configured_and_coordinates_exist(self) -> None:
        with mock.patch("urbanlens.dashboard.plugins.builtin.inaturalist.redata_configured", return_value=True):
            self.assertTrue(self.source.gate(self.pin))
