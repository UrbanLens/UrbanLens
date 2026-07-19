"""Tests for the iNaturalist panel's render_context().

Regression coverage for linking to specific observations/area instead of
iNaturalist's homepage - see docs/prompts/completed.md.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.plugins.builtin.inaturalist import INaturalistPanelSource

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
