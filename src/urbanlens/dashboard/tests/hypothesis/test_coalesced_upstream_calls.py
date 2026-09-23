"""Identical upstream asks made together cost one call.

Five panel sources on one Private Pin page each asked REData ``parcels/lookup`` for the same coordinate, in
separate tasks started within a second of each other: 205 parcel lookups in the HRSH run's hour for 70 pins.
"""

from __future__ import annotations

import threading
from unittest import mock

from django.core.cache import cache
import pytest

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.property_records.redata_gateway import RedataGateway
from urbanlens.dashboard.services.core.coalesce import coalesced


class CoalescedTests(SimpleTestCase):
    def test_a_second_ask_is_answered_from_the_first(self) -> None:
        compute = mock.Mock(return_value={"uuid": "p-1"})

        first = coalesced("t:parcel", compute, ttl=60)
        second = coalesced("t:parcel", compute, ttl=60)

        self.assertEqual(first, second)
        compute.assert_called_once()

    def test_none_is_an_answer_too(self) -> None:
        compute = mock.Mock(return_value=None)

        coalesced("t:none", compute, ttl=60)
        coalesced("t:none", compute, ttl=60)

        compute.assert_called_once()

    def test_a_concurrent_ask_waits_for_the_one_in_flight(self) -> None:
        started = threading.Event()
        release = threading.Event()
        calls = []

        def slow() -> str:
            calls.append(1)
            started.set()
            release.wait(5)
            return "answer"

        results: list[str] = []
        leader = threading.Thread(target=lambda: results.append(coalesced("t:slow", slow, ttl=60)))
        leader.start()
        started.wait(5)
        follower = threading.Thread(target=lambda: results.append(coalesced("t:slow", slow, ttl=60, poll_seconds=0.01)))
        follower.start()
        release.set()
        leader.join(5)
        follower.join(5)

        self.assertEqual(results, ["answer", "answer"])
        self.assertEqual(len(calls), 1)

    def test_a_failure_is_not_shared(self) -> None:
        compute = mock.Mock(side_effect=[RuntimeError("down"), "answer"])

        with pytest.raises(RuntimeError):
            coalesced("t:fail", compute, ttl=60)
        self.assertEqual(coalesced("t:fail", compute, ttl=60), "answer")

    def test_an_unreachable_cache_just_makes_the_call(self) -> None:
        compute = mock.Mock(return_value="answer")

        with (
            mock.patch.object(cache, "get", side_effect=ConnectionError),
            mock.patch.object(cache, "add", side_effect=ConnectionError),
            mock.patch.object(cache, "set", side_effect=ConnectionError),
        ):
            self.assertEqual(coalesced("t:down", compute, ttl=60), "answer")


class ParcelLookupTests(SimpleTestCase):
    """Every panel that needs the parcel asks the same question; REData answers it once."""

    def _gateway(self) -> tuple[RedataGateway, mock.Mock]:
        session = mock.Mock()
        response = mock.Mock(status_code=200, headers={})
        response.json.return_value = {"uuid": "parcel-1", "record_payload": {"owner": "State of New York"}}
        session.get.return_value = response
        return RedataGateway(base_url="https://redata.example.test", api_key="k", session=session), session

    def test_the_record_and_the_uuid_share_one_lookup(self) -> None:
        gateway, session = self._gateway()

        record = gateway.lookup_parcel(41.7321, -73.9262)
        uuid = RedataGateway(base_url="https://redata.example.test", api_key="k", session=session).lookup_parcel_uuid(
            41.7321, -73.9262
        )

        self.assertEqual(record["uuid"], "parcel-1")
        self.assertEqual(uuid, "parcel-1")
        session.get.assert_called_once()

    def test_a_different_address_is_a_different_question(self) -> None:
        gateway, session = self._gateway()

        gateway.lookup_parcel(41.7321, -73.9262)
        gateway.lookup_parcel(41.7321, -73.9262, situs_address="1 Main St")

        self.assertEqual(session.get.call_count, 2)


def _ok(body: object) -> mock.Mock:
    response = mock.Mock(status_code=200, headers={}, ok=True)
    response.json.return_value = body
    return response


class SiteLevelAnswersTests(SimpleTestCase):
    """Answers that describe the area, not the point, are asked once per area."""

    def test_the_capability_index_is_read_once_for_every_domain(self) -> None:
        """Satellite imagery, cultural resources and site features each asked for the same index."""
        from urbanlens.dashboard.services.apis.locations import redata_capabilities_gateway as capabilities

        index = {
            "domains": [
                {"tag": tag, "applicable_providers": [f"{tag}-a"]}
                for tag in ("imagery", "cultural_resources", "points_of_interest")
            ]
        }
        with mock.patch.object(capabilities.RedataCapabilitiesGateway, "get_capabilities", return_value=index) as fetch:
            tags = [
                capabilities.applicable_providers(tag, 41.7321, -73.9262)
                for tag in ("imagery", "cultural_resources", "points_of_interest")
            ]

        self.assertEqual(tags, [["imagery-a"], ["cultural_resources-a"], ["points_of_interest-a"]])
        fetch.assert_called_once()

    def test_the_nearest_park_is_shared_across_a_site(self) -> None:
        """Two buildings 60 m apart on one campus; the nearest park unit within 100 km is the same answer."""
        from urbanlens.dashboard.services.apis.locations.redata_national_parks_gateway import RedataNationalParksGateway

        session = mock.Mock()
        session.get.return_value = _ok(
            {"count": 1, "complete": True, "results": [{"park_code": "vama"}], "providers": []}
        )

        def gateway() -> RedataNationalParksGateway:
            return RedataNationalParksGateway(base_url="https://redata.example.test", api_key="k", session=session)

        first = gateway().find_nearest_park(41.7321, -73.9262)
        second = gateway().find_nearest_park(41.7326, -73.9258)

        self.assertEqual(first, second)
        session.get.assert_called_once()

    def test_a_repeated_nearby_place_search_is_one_call(self) -> None:
        """Name resolution asked the same 50 m question twice while creating one pin, and again on its page."""
        from urbanlens.dashboard.services.apis.locations.google.redata_places_gateway import RedataPlacesGateway

        session = mock.Mock()
        session.get.return_value = _ok({"count": 1, "results": [{"place_id": "p1", "name": "Kirkbride"}]})

        def gateway() -> RedataPlacesGateway:
            return RedataPlacesGateway(base_url="https://redata.example.test", api_key="k", session=session)

        gateway().search_nearby(41.7321, -73.9262, radius_meters=50)
        gateway().search_nearby(41.7321, -73.9262, radius_meters=50)
        gateway().search_nearby(41.7321, -73.9262, radius_meters=75, max_results=1)

        self.assertEqual(session.get.call_count, 2)
