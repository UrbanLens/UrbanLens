"""REData 0.3.7 answers a cold parcel's unfiltered buildings and boundaries with ``503 refresh_queued`` (its P62).

REData finishes the parcel's one computation in the background and names the wait in the body, not in a
``Retry-After`` header, because the wait is this parcel's and not the endpoint's. ``compute_timeout`` is the same
answer when the computation could not be queued or failed lately. UrbanLens honours the wait for that parcel: nothing
is cached meanwhile, no fallback stands in for an answer REData is about to have, and the parcel is asked again once
the wait is over.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.apis.locations.base import BoundaryProvider, BoundaryProviderDeferredError
from urbanlens.dashboard.services.apis.locations.boundaries.redata import RedataBoundaryProvider
from urbanlens.dashboard.services.apis.property_records.redata_gateway import (
    PropertyRecordsComputingError,
    PropertyRecordsUnavailableError,
    RedataGateway,
)
from urbanlens.dashboard.services.locations.site_scope import PARCEL_BUILDINGS_CACHE_SOURCE
from urbanlens.dashboard.services.pins.external_data import fetch_blocked, get_panel_source, run_panel_fetch
from urbanlens.dashboard.tests.hypothesis.redata_helpers import RedataConfiguredMixin

if TYPE_CHECKING:
    from django.contrib.gis.geos import Polygon

    from urbanlens.dashboard.services.locations.boundaries import ResolvedBoundaries

_PENDING = (("refresh_queued", 60), ("compute_timeout", 120))
_BUILDINGS = "urbanlens.dashboard.plugins.builtin.parcel_buildings"


def _response(status: int, body: object, headers: dict[str, str] | None = None) -> mock.Mock:
    response = mock.Mock(status_code=status, headers=headers or {}, text="")
    response.json.return_value = body
    return response


def _gateway(session: mock.Mock) -> RedataGateway:
    return RedataGateway(base_url="https://redata.example.test", api_key="test-key", session=session)


def _computing(retry_after: int = 60) -> PropertyRecordsComputingError:
    return PropertyRecordsComputingError(
        "refresh_queued", "The parcel's answers are being computed.", retry_after=retry_after
    )


class GatewayTests(SimpleTestCase):
    def test_a_parcel_being_computed_is_a_wait_of_the_bodys_length(self) -> None:
        for error, wait in _PENDING:
            for read in ("lookup_parcel_buildings", "lookup_boundaries"):
                with self.subTest(error=error, read=read):
                    session = mock.Mock()
                    session.get.return_value = _response(
                        503, {"error": error, "message": "computing", "retry_after": wait}
                    )

                    with self.assertRaises(PropertyRecordsComputingError) as raised:
                        getattr(_gateway(session), read)("parcel-uuid")

                    self.assertEqual(raised.exception.retry_after, wait)
                    self.assertEqual(raised.exception.reason, error)
                    self.assertTrue(raised.exception.is_outage)

    def test_a_body_that_names_no_wait_still_waits(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(503, {"error": "refresh_queued", "message": "computing"})

        with self.assertRaises(PropertyRecordsComputingError) as raised:
            _gateway(session).lookup_parcel_buildings("parcel-uuid")

        self.assertGreater(raised.exception.retry_after, 0)

    def test_a_parcel_being_computed_trips_no_breaker(self) -> None:
        """The wait is one parcel's, so REData sends it in the body; every other parcel is still asked."""
        from urbanlens.dashboard.services.core.upstream_breaker import RedataBreaker

        url = "https://redata.example.test/api/v1/parcels/parcel-uuid/buildings/"
        for error, wait in _PENDING:
            with self.subTest(error=error):
                answer = _response(503, {"error": error, "message": "computing", "retry_after": wait})

                self.assertIsNone(RedataBreaker().scope_tripped_by(url, None, answer))

    def test_another_503_is_not_taken_for_a_computation(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(
            503, {"error": "source_error", "message": "upstream timed out", "retry_after": 60}
        )

        with self.assertRaises(PropertyRecordsUnavailableError) as raised:
            _gateway(session).lookup_parcel_buildings("parcel-uuid")

        self.assertNotIsInstance(raised.exception, PropertyRecordsComputingError)


class BuildingListTests(RedataConfiguredMixin, TestCase):
    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        baker.make(User)  # absorbs the first-user site-admin promotion
        self.location = baker.make(Location, latitude="41.733000", longitude="-73.930000", google_place=None)
        self.pin = baker.make(Pin, profile=baker.make(User).profile, location=self.location)

    def _computing_redata(self) -> mock._patch:
        return mock.patch.multiple(
            RedataGateway,
            __post_init__=mock.Mock(return_value=None),
            lookup_parcel_uuid=mock.Mock(return_value="parcel-1"),
            lookup_parcel_buildings=mock.Mock(side_effect=_computing()),
        )

    def test_no_fallback_stands_in_for_an_answer_redata_is_computing(self) -> None:
        from urbanlens.dashboard.plugins.builtin.parcel_buildings import fetch_parcel_buildings

        with (
            self._computing_redata(),
            mock.patch(f"{_BUILDINGS}._overpass_buildings") as overpass,
            mock.patch(f"{_BUILDINGS}._cris_buildings") as cris,
            self.assertRaises(PropertyRecordsComputingError),
        ):
            fetch_parcel_buildings(self.location)

        overpass.assert_not_called()
        cris.assert_not_called()

    def test_the_panel_caches_nothing_and_waits_redatas_time(self) -> None:
        with self._computing_redata(), mock.patch(f"{_BUILDINGS}._overpass_buildings") as overpass:
            waited = run_panel_fetch(PARCEL_BUILDINGS_CACHE_SOURCE, self.pin)

        self.assertEqual(waited, 60)
        overpass.assert_not_called()
        self.assertFalse(
            LocationCache.objects.filter(location=self.location, source=PARCEL_BUILDINGS_CACHE_SOURCE).exists()
        )
        source = get_panel_source(PARCEL_BUILDINGS_CACHE_SOURCE)
        assert source is not None
        self.assertTrue(fetch_blocked(source, self.pin), "asked again once REData's wait is over, not on every poll")

    def test_a_throttle_is_skipped_as_before_not_waited_out_inline(self) -> None:
        """Review finding 4: only a computation's wait sends the bootstrap back to the same stage."""
        from urbanlens.dashboard.services.apis.property_records.redata_gateway import PropertyRecordsBusyError

        source = get_panel_source(PARCEL_BUILDINGS_CACHE_SOURCE)
        assert source is not None
        throttle = PropertyRecordsBusyError("rate_limited", "Throttled.", retry_after=60)
        with mock.patch.object(type(source), "fetch", side_effect=throttle):
            waited = run_panel_fetch(PARCEL_BUILDINGS_CACHE_SOURCE, self.pin)

        self.assertIsNone(waited)
        self.assertTrue(fetch_blocked(source, self.pin), "still left alone for the wait REData named")


@dataclass(slots=True, kw_only=True)
class _Deferring(BoundaryProvider):
    """A boundary provider that defers as it is told."""

    deferral: BoundaryProviderDeferredError

    def get_boundary(self, latitude: float, longitude: float, *, name: str | None = None) -> Polygon | None:
        raise self.deferral


class BoundaryTests(TestCase):
    def test_a_parcel_being_computed_defers_for_redatas_wait(self) -> None:
        gateway = mock.Mock()
        gateway.lookup_boundaries.side_effect = _computing(90)
        gateway.lookup_parcel_buildings.side_effect = _computing(90)
        provider = RedataBoundaryProvider()

        for ask in (
            lambda: provider._scored_boundary(gateway, "parcel-1"),
            lambda: provider._buildings_convex_hull(gateway, "parcel-1", 41.733, -73.93),
        ):
            with self.subTest(ask=ask), self.assertRaises(BoundaryProviderDeferredError) as raised:
                ask()
            self.assertEqual(raised.exception.retry_after, 90)
            self.assertTrue(raised.exception.computing)

    def test_an_outage_still_defers_as_a_back_off(self) -> None:
        gateway = mock.Mock()
        gateway.lookup_boundaries.side_effect = PropertyRecordsUnavailableError(
            "source_error", "down", retry_later=True
        )

        with self.assertRaises(BoundaryProviderDeferredError) as raised:
            RedataBoundaryProvider()._scored_boundary(gateway, "parcel-1")

        self.assertFalse(raised.exception.computing)

    def _chain(self, *deferrals: BoundaryProviderDeferredError) -> ResolvedBoundaries:
        from urbanlens.dashboard.services.locations.boundaries import BoundaryProviderChain

        providers = tuple(_Deferring(deferral=deferral) for deferral in deferrals)
        return BoundaryProviderChain(providers=providers).get_boundaries(41.733, -73.93)

    def test_a_chain_waiting_only_on_a_computation_does_not_back_off(self) -> None:
        resolved = self._chain(BoundaryProviderDeferredError("redata_boundary", retry_after=60, computing=True))

        self.assertEqual((resolved.retry_after, resolved.backs_off), (60, False))

    def test_any_other_deferral_backs_off(self) -> None:
        resolved = self._chain(
            BoundaryProviderDeferredError("redata_boundary", retry_after=60, computing=True),
            BoundaryProviderDeferredError("overture", retry_after=30),
        )

        self.assertTrue(resolved.backs_off)

    def _retry(self, *, backs_off: bool) -> int:
        from urbanlens.dashboard.services.locations import boundaries

        location = baker.make(Location, latitude="41.733000", longitude="-73.930000", google_place=None)
        with mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue:
            boundaries._schedule_deferred_retry(location, 60, attempt=0, force=False, backs_off=backs_off)
        return enqueue.call_args.kwargs["countdown"]

    def test_the_retry_waits_redatas_time_for_a_computation(self) -> None:
        self.assertEqual(self._retry(backs_off=False), 60)

    def test_the_retry_backs_off_for_anything_else(self) -> None:
        from urbanlens.dashboard.services.locations.boundaries import DEFERRED_RETRY_BASE_SECONDS

        self.assertEqual(self._retry(backs_off=True), DEFERRED_RETRY_BASE_SECONDS)
