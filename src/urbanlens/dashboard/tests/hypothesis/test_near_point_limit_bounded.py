"""A near-point ``limit`` bounds what UrbanLens keeps, whether or not REData applied it.

REData's near-point endpoints parsed ``limit`` without applying it, so every row in the radius was written into the
``LocationCache`` JSON, and a panel's "most recent N" was really "all of them".
"""

from __future__ import annotations

from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import RedataLocationContextGateway
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
