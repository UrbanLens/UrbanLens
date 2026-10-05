"""A near-point ``limit`` bounds what UrbanLens keeps, whether or not REData applied it.

REData's near-point endpoints parsed ``limit`` without applying it, so every row in the radius was written into the
``LocationCache`` JSON, and a panel's "most recent N" was really "all of them".
"""

from __future__ import annotations

from typing import Any
from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.plugins.builtin.hazard_history import HazardHistoryPanelSource
from urbanlens.dashboard.plugins.builtin.inaturalist import INaturalistPanelSource
from urbanlens.dashboard.plugins.builtin.redata_air_quality import AirQualityPanelSource
from urbanlens.dashboard.plugins.builtin.redata_historical_features import HistoricalFeaturesPanelSource
from urbanlens.dashboard.plugins.builtin.redata_hydrology import HydrologyPanelSource
from urbanlens.dashboard.plugins.builtin.redata_incidents import IncidentHistoryPanelSource, PoliceIncidentsPanelSource
from urbanlens.dashboard.plugins.builtin.redata_permits import BuildingPermitsPanelSource
from urbanlens.dashboard.plugins.builtin.redata_site_features import SiteFeaturesPanelSource
from urbanlens.dashboard.plugins.builtin.redata_underground import UndergroundPanelSource
from urbanlens.dashboard.plugins.builtin.usgs_earthquakes import UsgsEarthquakePanelSource
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import (
    MAX_NEAR_POINT_LIMIT,
    RedataLocationContextGateway,
)
from urbanlens.dashboard.services.apis.locations.redata_media_gateway import RedataMediaGateway


def _response(body: dict) -> mock.Mock:
    response = mock.Mock(status_code=200)
    response.json.return_value = body
    response.text = ""
    return response


def _envelope(rows: list[dict]) -> dict:
    return {"count": len(rows), "complete": True, "results": rows, "providers": []}


class NearPointLimitTests(SimpleTestCase):
    def test_the_limit_is_sent(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(_envelope([]))
        gateway = RedataLocationContextGateway(base_url="https://redata.example.test", api_key="k", session=session)

        gateway.near_point("/api/v1/permits/", 40.0, -74.0, limit=25)

        self.assertEqual(session.get.call_args.kwargs["params"]["limit"], 25)

    def test_rows_past_the_limit_are_dropped(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(_envelope([{"id": index} for index in range(40)]))
        gateway = RedataLocationContextGateway(base_url="https://redata.example.test", api_key="k", session=session)

        envelope = gateway.near_point("/api/v1/permits/", 40.0, -74.0, limit=25)

        self.assertEqual([row["id"] for row in envelope.results], list(range(25)))
        self.assertEqual(envelope.count, 25)

    def test_a_limit_past_redatas_ceiling_is_sent_as_the_ceiling(self) -> None:
        """REData answers 400 for a ``limit`` above 200, which hides the panel that asked for it."""
        session = mock.Mock()
        session.get.return_value = _response(_envelope([]))
        gateway = RedataLocationContextGateway(base_url="https://redata.example.test", api_key="k", session=session)

        gateway.near_point("/api/v1/incidents/", 40.0, -74.0, limit=500)

        self.assertEqual(session.get.call_args.kwargs["params"]["limit"], MAX_NEAR_POINT_LIMIT)

    def test_no_limit_keeps_every_row(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(_envelope([{"id": index} for index in range(40)]))
        gateway = RedataLocationContextGateway(base_url="https://redata.example.test", api_key="k", session=session)

        self.assertEqual(len(gateway.near_point("/api/v1/permits/", 40.0, -74.0).results), 40)


class AerialMediaLimitTests(SimpleTestCase):
    """``is_aerial`` is filtered here, so a REData-side limit must not decide which rows reach the filter."""

    def test_the_aerial_filter_sees_more_than_the_tiles_it_keeps(self) -> None:
        rows = [{"id": index, "is_aerial": index % 10 == 0} for index in range(200)]
        session = mock.Mock()
        session.get.return_value = _response(_envelope(rows))
        gateway = RedataMediaGateway(base_url="https://redata.example.test", api_key="k", session=session)

        items = gateway.lookup(40.0, -74.0, is_aerial=True, limit=5)

        self.assertEqual([item["id"] for item in items], [0, 10, 20, 30, 40])
        self.assertGreater(session.get.call_args.kwargs["params"]["limit"], 5)


#: One minimal row per panel that its render_context counts, and the panel's own limit.
_PANELS: tuple[tuple[Any, dict], ...] = (
    (HistoricalFeaturesPanelSource(), {"kind": "building", "name": "Mill", "start_year": 1900}),
    (UndergroundPanelSource(), {"kind": "tunnel", "name": "Tunnel"}),
    (HydrologyPanelSource(), {"kind": "stream", "name": "Brook", "distance_meters": 10}),
    (BuildingPermitsPanelSource(), {"kind": "building", "issued_at": "2020-01-01"}),
    (SiteFeaturesPanelSource(), {"category": "Camera", "name": "Camera", "url": ""}),
    (AirQualityPanelSource(), {"source_kind": "sensor", "observed_at": "2026-10-05T14:00:00Z"}),
    (HazardHistoryPanelSource(), {"provider": "nifc_wildfires", "occurred_at": "2020-01-01"}),
    (INaturalistPanelSource(), {"common_name": "Robin"}),
    (PoliceIncidentsPanelSource(), {"category": "theft", "occurred_at": "2026-01-01"}),
    (IncidentHistoryPanelSource(), {"category": "theft", "occurred_at": "2026-01-01"}),
    (UsgsEarthquakePanelSource(), {"event_type": "earthquake", "occurred_at": "2020-01-01", "magnitude": 3.5}),
)


def _payload_key(source: Any) -> str:
    return (
        getattr(source, "payload_key", None) or {"hazard_history": "events", "inaturalist": "observations"}[source.key]
    )


def _qualified(chips: list[str]) -> bool:
    return any("+" in chip or "more than shown" in chip for chip in chips)


class FullAnswersAreCountedAsFloorsTests(TestCase):
    """A panel that asked for N rows and got N cannot say there are N."""

    def setUp(self) -> None:
        super().setUp()
        location = baker.make("dashboard.Location", latitude=40.5, longitude=-74.5)
        self.pin = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile, location=location)

    def test_every_counting_panel_declares_a_limit_redata_accepts(self) -> None:
        for source, _row in _PANELS:
            with self.subTest(source=source.key):
                self.assertIsNotNone(source.row_limit)
                self.assertLessEqual(source.row_limit, MAX_NEAR_POINT_LIMIT)

    def test_a_full_answer_is_qualified(self) -> None:
        for source, row in _PANELS:
            with self.subTest(source=source.key):
                context = source.render_context(
                    self.pin, {_payload_key(source): [dict(row) for _ in range(source.row_limit)]}
                )

                assert context is not None
                self.assertTrue(_qualified(context["chips"]), context["chips"])

    def test_a_short_answer_is_a_total(self) -> None:
        for source, row in _PANELS:
            with self.subTest(source=source.key):
                context = source.render_context(
                    self.pin, {_payload_key(source): [dict(row) for _ in range(source.row_limit - 1)]}
                )

                assert context is not None
                self.assertFalse(_qualified(context["chips"]), context["chips"])
