"""A parcel's owners, sales, tax payments and liens are read across REData's pages, not just the first.

These four endpoints are page-number paginated (100 rows a page by default). A parcel with a long tax history lost
everything past the first page, which is exactly the delinquency record the card exists to show.
"""

from __future__ import annotations

from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.property_records.redata_gateway import (
    MAX_PARCEL_ROW_PAGES,
    PropertyRecordsUnavailableError,
    RedataGateway,
)

_PARCEL = "3fae2b1c-0000-0000-0000-000000000000"
_BASE = "https://redata.example.test"


def _page(rows: list[dict], *, page: int, last: bool) -> mock.Mock:
    response = mock.Mock(status_code=200, headers={})
    response.text = ""
    response.json.return_value = {
        "count": 0,
        # REData builds these from its own request, so their host is not necessarily the one UrbanLens calls.
        "next": None if last else f"http://internal-redata:8000/api/v1/parcels/{_PARCEL}/owners/?page={page + 1}",
        "previous": None,
        "results": rows,
    }
    return response


def _gateway(session: mock.Mock) -> RedataGateway:
    return RedataGateway(base_url=_BASE, api_key="test-key", session=session)


class PaginatedParcelRowsTests(SimpleTestCase):
    def test_every_page_is_read(self) -> None:
        for method in ("lookup_owners", "lookup_sales", "lookup_tax_payments", "lookup_liens"):
            with self.subTest(method=method):
                session = mock.Mock()
                session.get.side_effect = [
                    _page([{"id": 1}, {"id": 2}], page=1, last=False),
                    _page([{"id": 3}], page=2, last=True),
                ]

                rows = getattr(_gateway(session), method)(_PARCEL)

                self.assertEqual([row["id"] for row in rows], [1, 2, 3])

    def test_later_pages_are_asked_of_the_configured_host(self) -> None:
        """``next`` names whatever host REData saw; following it would send the API key there."""
        session = mock.Mock()
        session.get.side_effect = [_page([{"id": 1}], page=1, last=False), _page([{"id": 2}], page=2, last=True)]

        _gateway(session).lookup_owners(_PARCEL)

        second = session.get.call_args_list[1]
        self.assertTrue(second.args[0].startswith(f"{_BASE}/api/v1/parcels/{_PARCEL}/owners/"))
        self.assertEqual(second.kwargs["params"]["page"], 2)

    def test_pages_are_as_large_as_redata_allows(self) -> None:
        session = mock.Mock()
        session.get.return_value = _page([], page=1, last=True)

        _gateway(session).lookup_liens(_PARCEL)

        self.assertEqual(session.get.call_args.kwargs["params"]["page_size"], 500)

    def test_reading_stops_at_the_page_cap(self) -> None:
        session = mock.Mock()
        session.get.side_effect = [_page([{"id": page}], page=page, last=False) for page in range(1, 50)]

        rows = _gateway(session).lookup_tax_payments(_PARCEL)

        self.assertEqual(session.get.call_count, MAX_PARCEL_ROW_PAGES)
        self.assertEqual(len(rows), MAX_PARCEL_ROW_PAGES)

    def test_a_failed_later_page_fails_the_read(self) -> None:
        """Half a tax history read as the whole one would understate delinquency."""
        failure = mock.Mock(status_code=503, headers={})
        failure.text = ""
        failure.json.return_value = {"error": "source_error", "message": ""}
        session = mock.Mock()
        session.get.side_effect = [_page([{"id": 1}], page=1, last=False), failure]

        with self.assertRaises(PropertyRecordsUnavailableError):
            _gateway(session).lookup_tax_payments(_PARCEL)
