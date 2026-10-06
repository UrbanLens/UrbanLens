"""A REData answer that says "ask again later" is never cached as REData's settled answer about the place.

REData answers ``parcels/lookup`` with a 404 for a permanent reason and a 503 for every other one, and answers
``cultural-resources/lookup`` with a 503 ``all_providers_unavailable`` or ``rate_limited`` when no register could be
asked. A ``LocationCache`` row marks a source fetched for the whole cache window, so storing either as "no record"
turns a passing state into a week-long blank card.

The same holds when the budget that said no was the requesting key's share of REData's: ``key_budget_exhausted``.
"""

from __future__ import annotations

from unittest import mock
from unittest.mock import MagicMock

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.apis.property_records.redata_gateway import (
    PropertyRecordsBusyError,
    PropertyRecordsUnavailableError,
    RedataGateway,
)
from urbanlens.dashboard.services.core.gateway import UpstreamBusyError, is_source_outage
from urbanlens.dashboard.tests.hypothesis.redata_helpers import BUDGET_REFUSALS, RedataConfiguredMixin


def _response(status_code: int, body: object, *, headers: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.text = ""
    resp.json.return_value = body
    return resp


def _gateway(session: MagicMock) -> RedataGateway:
    return RedataGateway(base_url="https://redata.example.test", api_key="test-key", session=session)


class CulturalResourceBlackoutTests(SimpleTestCase):
    def _lookup(self, response: MagicMock) -> PropertyRecordsUnavailableError:
        session = MagicMock()
        session.get.return_value = response
        with self.assertRaises(PropertyRecordsUnavailableError) as raised:
            _gateway(session).lookup_cultural_resources(42.65, -73.75, provider="ny_cris")
        return raised.exception

    def test_every_register_unavailable_is_an_outage(self) -> None:
        error = self._lookup(_response(503, {"error": "all_providers_unavailable", "message": "", "providers": []}))

        self.assertTrue(error.is_outage)
        self.assertTrue(is_source_outage(error))

    def test_every_register_out_of_budget_is_an_outage(self) -> None:
        for refusal in BUDGET_REFUSALS:
            with self.subTest(refusal):
                error = self._lookup(_response(503, {"error": refusal, "message": "", "providers": []}))

                self.assertTrue(error.is_outage)
                self.assertTrue(is_source_outage(error))

    def test_an_empty_answer_with_a_register_out_of_budget_is_an_outage(self) -> None:
        for status in BUDGET_REFUSALS:
            with self.subTest(status):
                error = self._lookup(
                    _response(
                        200,
                        {
                            "count": 0,
                            "complete": False,
                            "results": [],
                            "providers": [{"provider": "ny_cris", "status": status, "count": 0}],
                        },
                    )
                )

                self.assertTrue(error.is_outage)

    def test_an_empty_answer_with_a_register_unasked_is_an_outage(self) -> None:
        """REData's envelope says ``complete: false`` when a register covering the point did not answer."""
        error = self._lookup(
            _response(
                200,
                {
                    "count": 0,
                    "complete": False,
                    "results": [],
                    "providers": [{"provider": "ny_cris", "status": "unavailable", "count": 0}],
                },
            )
        )

        self.assertTrue(error.is_outage)

    def test_a_complete_empty_answer_is_settled(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(200, {"count": 0, "complete": True, "results": [], "providers": []})

        self.assertEqual(_gateway(session).lookup_cultural_resources(42.65, -73.75, provider="ny_cris"), [])


class ParcelLookupRetryLaterTests(SimpleTestCase):
    def _lookup(self, session: MagicMock) -> PropertyRecordsUnavailableError:
        with self.assertRaises(PropertyRecordsUnavailableError) as raised:
            _gateway(session).lookup_parcel(42.65, -73.75)
        return raised.exception

    def test_a_permanent_404_is_settled(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(404, {"error": "outside_coverage", "message": "no county source"})

        self.assertFalse(self._lookup(session).is_outage)

    def test_a_503_is_never_settled_whatever_its_reason(self) -> None:
        reasons = (
            "no_data_found",
            "search_key_unavailable",
            "derived_address_unconfirmed",
            "a_reason_added_later",
            "source_rate_limited",
            *BUDGET_REFUSALS,
        )
        for offset, reason in enumerate(reasons):
            with self.subTest(reason=reason):
                session = MagicMock()
                session.get.return_value = _response(503, {"error": reason, "message": ""})

                # Each at its own point: a deferred question is answered from its deferral.
                with self.assertRaises(PropertyRecordsUnavailableError) as raised:
                    _gateway(session).lookup_parcel(42.65 + offset, -73.75)
                error = raised.exception

                self.assertEqual(error.reason, reason)
                self.assertTrue(error.is_outage)

    def test_a_named_wait_is_honoured(self) -> None:
        for reason in ("source_rate_limited", *BUDGET_REFUSALS):
            with self.subTest(reason):
                session = MagicMock()
                session.get.return_value = _response(
                    503, {"error": reason, "message": ""}, headers={"Retry-After": "120"}
                )

                error = self._lookup(session)

                self.assertIsInstance(error, PropertyRecordsBusyError)
                self.assertEqual(error.retry_after, 120)

    def test_a_spent_budget_is_asked_about_again_rather_than_deferred_for_hours(self) -> None:
        """An unsettled question waits hours; a spent budget frees on its own schedule, so the next call asks REData."""
        for offset, reason in enumerate(("source_rate_limited", *BUDGET_REFUSALS)):
            with self.subTest(reason):
                session = MagicMock()
                session.get.return_value = _response(503, {"error": reason, "message": ""})

                with self.assertRaises(PropertyRecordsUnavailableError):
                    _gateway(session).lookup_parcel(43.65 + offset, -73.75)
                with self.assertRaises(PropertyRecordsUnavailableError) as again:
                    _gateway(session).lookup_parcel(43.65 + offset, -73.75)

                self.assertEqual(session.get.call_count, 2)
                self.assertEqual(again.exception.reason, reason)
                self.assertTrue(again.exception.is_outage)

    def test_nothing_learned_waits_hours_rather_than_minutes(self) -> None:
        """No county source found the parcel; asking again in five minutes re-runs REData's whole tier pipeline."""
        session = MagicMock()
        session.get.return_value = _response(503, {"error": "no_data_found", "message": ""})

        error = self._lookup(session)

        self.assertIsInstance(error, UpstreamBusyError)
        self.assertGreaterEqual(error.retry_after, 3600)

    def test_the_same_question_is_not_asked_again_while_deferred(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(503, {"error": "no_data_found", "message": ""})

        self._lookup(session)
        again = self._lookup(session)

        self.assertEqual(session.get.call_count, 1)
        self.assertEqual(again.reason, "no_data_found")
        self.assertIsInstance(again, PropertyRecordsBusyError)

    def test_another_point_is_still_asked(self) -> None:
        session = MagicMock()
        session.get.return_value = _response(503, {"error": "no_data_found", "message": ""})
        self._lookup(session)

        session.get.return_value = _response(
            200, {"uuid": "3fae2b1c-0000-0000-0000-000000000000", "record_payload": {}}
        )
        payload = _gateway(session).lookup_parcel(40.0, -74.0)

        self.assertEqual(payload["uuid"], "3fae2b1c-0000-0000-0000-000000000000")


def _redata_answering(session: MagicMock):
    """Every ``RedataGateway()`` the code under test builds talks to ``session``."""
    return mock.patch(
        "urbanlens.dashboard.services.apis.property_records.redata_gateway.RedataGateway",
        side_effect=lambda *args, **kwargs: _gateway(session),
    )


class RetryLaterIsNotCachedTests(RedataConfiguredMixin, TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        profile = baker.make(User).profile
        location = baker.make(Location, latitude=41.7321, longitude=-73.9262, official_name="HRSH")
        self.pin = baker.make(Pin, profile=profile, location=location, parent_pin=None, name="HRSH")

    def _rows(self, source: str) -> int:
        return LocationCache.objects.filter(location=self.pin.location, source=source).count()

    def test_the_parcel_card_caches_nothing_for_a_retry_later_answer(self) -> None:
        from urbanlens.dashboard.plugins.builtin.property_records import PropertyRecordsPanelSource

        session = MagicMock()
        session.get.return_value = _response(503, {"error": "no_data_found", "message": ""})
        with _redata_answering(session), self.assertRaises(PropertyRecordsUnavailableError):
            PropertyRecordsPanelSource().fetch(self.pin)

        self.assertEqual(self._rows("property_records"), 0)

    def test_the_parcel_card_caches_nothing_for_a_spent_budget(self) -> None:
        from urbanlens.dashboard.plugins.builtin.property_records import PropertyRecordsPanelSource

        for reason in ("source_rate_limited", *BUDGET_REFUSALS):
            for headers in ({}, {"Retry-After": "120"}):
                with self.subTest(reason, headers=headers):
                    session = MagicMock()
                    session.get.return_value = _response(503, {"error": reason, "message": ""}, headers=headers)
                    with _redata_answering(session), self.assertRaises(PropertyRecordsUnavailableError):
                        PropertyRecordsPanelSource().fetch(self.pin)

                    self.assertEqual(self._rows("property_records"), 0)

    def test_the_parcel_card_still_caches_a_permanent_answer(self) -> None:
        from urbanlens.dashboard.plugins.builtin.property_records import PropertyRecordsPanelSource

        session = MagicMock()
        session.get.return_value = _response(404, {"error": "manual_only", "message": "Call the assessor"})
        with _redata_answering(session):
            PropertyRecordsPanelSource().fetch(self.pin)

        self.assertEqual(
            LocationCache.objects.get(location=self.pin.location, source="property_records").data["reason"],
            "manual_only",
        )

    def test_the_cris_card_caches_nothing_when_no_register_answered(self) -> None:
        from urbanlens.dashboard.plugins.builtin.cris_buildings import CrisBuildingPanelSource

        session = MagicMock()
        session.get.return_value = _response(
            503, {"error": "all_providers_unavailable", "message": "", "providers": []}
        )
        with _redata_answering(session), self.assertRaises(PropertyRecordsUnavailableError):
            CrisBuildingPanelSource().fetch(self.pin)

        self.assertEqual(self._rows("cris_building_usn"), 0)
