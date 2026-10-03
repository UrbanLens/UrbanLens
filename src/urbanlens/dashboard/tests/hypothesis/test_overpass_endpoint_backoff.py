"""A transient Overpass failure takes an endpoint out for minutes, growing on repeats, not until the next day (P205)."""

from __future__ import annotations

from unittest import mock

from django.core.cache import cache

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations.boundaries import overpass
from urbanlens.dashboard.services.apis.locations.boundaries.overpass import OverpassGateway

_QUERY = "[out:json];node(1);out;"


def _response(status_code: int, payload: dict | None = None, headers: dict[str, str] | None = None) -> mock.Mock:
    response = mock.Mock()
    response.status_code = status_code
    response.headers = headers or {}
    response.text = ""
    response.json.return_value = {"elements": [{"id": 1}]} if payload is None else payload
    return response


class EndpointBackoffTests(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        for patcher in (
            mock.patch.object(overpass.random, "shuffle", side_effect=lambda seq: None),
            mock.patch.object(overpass.time, "sleep"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.gateway = OverpassGateway(session=mock.Mock())
        self.primary = self.gateway.base_url

    def _down_for(self, *responses: mock.Mock) -> list[int]:
        """Run one query per primary response (a healthy mirror answers after each failure); the primary's down times."""
        downs: list[int] = []
        real_set = cache.set

        def record(key: str, value: object, timeout: int | None = None) -> None:
            if key == overpass._DOWN_CACHE_KEY.format(self.primary):
                downs.append(int(timeout or 0))
            real_set(key, value, timeout=timeout)

        with mock.patch.object(overpass.cache, "set", side_effect=record):
            for response in responses:
                cache.delete(overpass._DOWN_CACHE_KEY.format(self.primary))
                self.gateway.session.post.side_effect = [response, _response(200)]
                self.gateway.query(_QUERY)
        return downs

    def test_one_504_takes_the_endpoint_out_for_minutes(self) -> None:
        self.assertEqual(self._down_for(_response(504)), [overpass.DOWN_BASE_SECONDS])
        self.assertLessEqual(overpass.DOWN_BASE_SECONDS, 300)

    def test_repeated_failures_back_off_further_up_to_a_cap(self) -> None:
        downs = self._down_for(*[_response(502) for _ in range(10)])

        self.assertEqual(
            downs[:3], [overpass.DOWN_BASE_SECONDS, 2 * overpass.DOWN_BASE_SECONDS, 4 * overpass.DOWN_BASE_SECONDS]
        )
        self.assertEqual(downs[-1], overpass.DOWN_CAP_SECONDS)
        self.assertLess(overpass.DOWN_CAP_SECONDS, 86_400)

    def test_an_answer_from_the_endpoint_resets_its_backoff(self) -> None:
        self._down_for(_response(504), _response(504))
        self.gateway.session.post.side_effect = [_response(200)]
        cache.delete(overpass._DOWN_CACHE_KEY.format(self.primary))
        self.gateway.query(_QUERY)

        self.assertEqual(self._down_for(_response(504)), [overpass.DOWN_BASE_SECONDS])

    def test_a_stated_wait_is_honoured_up_to_a_day(self) -> None:
        self.assertEqual(self._down_for(_response(429, headers={"Retry-After": "7200"})), [7200])
        self.assertEqual(self._down_for(_response(503, headers={"Retry-After": "999999"})), [86_400])

    def test_a_connection_failure_backs_off_the_same_way(self) -> None:
        import requests

        downs: list[int] = []
        real_set = cache.set

        def record(key: str, value: object, timeout: int | None = None) -> None:
            if key == overpass._DOWN_CACHE_KEY.format(self.primary):
                downs.append(int(timeout or 0))
            real_set(key, value, timeout=timeout)

        self.gateway.session.post.side_effect = [requests.ConnectionError("refused"), _response(200)]
        with mock.patch.object(overpass.cache, "set", side_effect=record):
            self.gateway.query(_QUERY)

        self.assertEqual(downs, [overpass.DOWN_BASE_SECONDS])

    def test_an_endpoint_caught_omitting_data_is_still_out_until_the_next_day(self) -> None:
        mirror = self.gateway.mirrors[0]
        overpass._mark_endpoint_down(self.primary)
        self.gateway.session.post.side_effect = [
            _response(200, {"elements": []}),
            _response(200, {"elements": [{"id": 1}]}),
        ]
        with (
            mock.patch.object(overpass, "_seconds_until_next_day", return_value=40_000),
            mock.patch.object(overpass.cache, "set", wraps=cache.set) as recorded,
        ):
            self.gateway.query(_QUERY)

        self.assertIn(mock.call(overpass._DOWN_CACHE_KEY.format(mirror), 1, timeout=40_000), recorded.call_args_list)
