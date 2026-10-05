"""The Seismic panel asks REData's hazards registry for earthquakes only.

Asked unfiltered, REData also ran its wildfire and FEMA providers for every US pin, applied ``min_magnitude`` to
burned acres, and an outage of either left the panel's earthquake answer incomplete, so it was never cached.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.plugins.builtin.usgs_earthquakes import UsgsEarthquakePanelSource
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope
from urbanlens.dashboard.tests.hypothesis.redata_helpers import RedataConfiguredMixin

_GATEWAY = "urbanlens.dashboard.services.apis.locations.redata_hazards_gateway.RedataHazardsGateway"


def _quake(index: int) -> dict:
    return {
        "event_type": "earthquake",
        "magnitude": 3.1,
        "title": f"{index} km N of Nowhere",
        "occurred_at": "2026-01-01T00:00:00Z",
        "url": "",
    }


class SeismicPanelTests(RedataConfiguredMixin, TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = UsgsEarthquakePanelSource()
        location = baker.make("dashboard.Location", latitude=40.5, longitude=-74.5)
        self.pin = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile, location=location)

    def test_only_the_earthquake_provider_is_asked(self) -> None:
        with mock.patch(_GATEWAY) as gateway_cls:
            gateway_cls.return_value.get_hazard_events.return_value = LocationContextEnvelope(
                count=0, complete=True, results=[]
            )
            self.source.fetch(self.pin)

        _, kwargs = gateway_cls.return_value.get_hazard_events.call_args
        self.assertEqual(kwargs["providers"], ["usgs_earthquakes"])

    def test_a_complete_earthquake_answer_is_cached(self) -> None:
        with mock.patch(_GATEWAY) as gateway_cls:
            gateway_cls.return_value.get_hazard_events.return_value = LocationContextEnvelope(
                count=1,
                complete=True,
                results=[_quake(1)],
                providers=[{"provider": "usgs_earthquakes", "status": "ok", "count": 1}],
            )
            self.source.fetch(self.pin)

        row = LocationCache.get_fresh(self.pin.location, self.source.cache_source)
        assert row is not None
        self.assertEqual(len(row.data["events"]), 1)

    def test_a_full_page_is_not_presented_as_the_total(self) -> None:
        context = self.source.render_context(
            self.pin, {"events": [_quake(index) for index in range(self.source.row_limit)]}
        )

        assert context is not None
        self.assertTrue(context["chips"][0].startswith(f"{self.source.row_limit}+ "), context["chips"])
