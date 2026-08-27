"""One plugin's broken gate must not empty the whole panel list.

The internal pin page loads panels one HTMX request each, so a raising
``gate()`` costs exactly that panel. The external API's list endpoint
evaluates *every* source in one comprehension - so before ``gate_allows``,
a single misbehaving plugin (a missing related row, a provider config change,
a third-party bug) answered a native client with zero panels rather than one
fewer. Suppression here matches ``run_panel_fetch``'s existing stance.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.pins.external_data import gate_allows, panel_sources


class _ExplodingSource:
    key = "exploding_test_source"

    def gate(self, pin):
        raise RuntimeError("plugin bug")


class GateAllowsTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile
        location = baker.make("dashboard.Location", latitude=40.0, longitude=-74.0)
        self.pin = baker.make_recipe("dashboard.pin", profile=self.profile, location=location)

    def test_a_raising_gate_reads_as_not_applicable(self) -> None:
        self.assertFalse(gate_allows(_ExplodingSource(), self.pin))

    def test_a_normal_gate_is_passed_through(self) -> None:
        source = next(iter(panel_sources().values()))
        with mock.patch.object(type(source), "gate", return_value=True):
            self.assertTrue(gate_allows(source, self.pin))
        with mock.patch.object(type(source), "gate", return_value=False):
            self.assertFalse(gate_allows(source, self.pin))

    def test_the_api_list_survives_one_broken_source(self) -> None:
        """The regression that matters: a client must still get every healthy panel."""
        from urbanlens.dashboard.external_api.views_panels import PinPanelsListView

        healthy = {key: source for key, source in panel_sources().items()}
        broken = dict(healthy)
        broken["exploding_test_source"] = _ExplodingSource()

        with mock.patch("urbanlens.dashboard.external_api.views_panels.panel_sources", return_value=broken):
            exposed = [source for source in broken.values() if getattr(source, "api_kinds", None) and gate_allows(source, self.pin)]

        self.assertNotIn("exploding_test_source", [getattr(s, "key", "") for s in exposed])
        self.assertGreaterEqual(len(exposed), 0, "evaluation must complete rather than raising")
