"""Tests for RedataCidGateway.resolve_cids against REData's shipped contract
(``../REData/docs/api-reference.md``, "Google Maps CID resolution").

Constructs the gateway with a mock ``session`` (Gateway.__post_init__ leaves a
non-default session untouched, skipping the DB-backed rate-limiting wrapper -
see gateway.py) so these stay pure unit tests with no database access.
"""

from __future__ import annotations

import json
from unittest import mock

import pytest

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations.google import redata_cid_gateway as gw_module
from urbanlens.dashboard.services.apis.locations.google.redata_cid_gateway import CidLookupEntry, RedataCidGateway, RedataPermissionError
from urbanlens.dashboard.services.core.gateway import GatewayRequestError


def _response(status_code: int, body: dict) -> mock.Mock:
    resp = mock.Mock(status_code=status_code)
    resp.json.return_value = body
    resp.text = json.dumps(body)
    return resp


class RedataCidGatewayResolveCidsTests(SimpleTestCase):
    def _gateway(self, session: mock.Mock) -> RedataCidGateway:
        return RedataCidGateway(base_url="https://redata.example.test", api_key="test-key", session=session)

    def test_parses_results_and_pending_into_the_three_buckets(self) -> None:
        session = mock.Mock()
        session.post.return_value = _response(
            200,
            {
                "results": {"1": {"lat": 38.456, "lng": -77.123}, "2": None},
                "pending": ["3"],
            },
        )

        result = self._gateway(session).resolve_cids([1, 2, 3])

        self.assertEqual(result.resolved, {1: (38.456, -77.123)})
        self.assertEqual(result.unresolvable, {2})
        self.assertEqual(result.pending, {3})

    def test_cid_missing_from_both_buckets_defaults_to_pending(self) -> None:
        """A cid REData's response doesn't mention at all is never silently dropped."""
        session = mock.Mock()
        session.post.return_value = _response(200, {"results": {}, "pending": []})

        result = self._gateway(session).resolve_cids([42])

        self.assertEqual(result.pending, {42})

    def test_non_200_raises_gateway_request_error(self) -> None:
        session = mock.Mock()
        session.post.return_value = _response(503, {})

        with pytest.raises(GatewayRequestError) as exc_info:
            self._gateway(session).resolve_cids([1])
        self.assertNotIsInstance(exc_info.value, RedataPermissionError)

    def test_403_raises_redata_permission_error_not_plain_gateway_error(self) -> None:
        """A missing/insufficient key scope will never succeed by retrying - must be distinguishable."""
        session = mock.Mock()
        session.post.return_value = _response(403, {"detail": "You do not have permission to perform this action."})

        with pytest.raises(RedataPermissionError):
            self._gateway(session).resolve_cids([1])

    def test_401_raises_redata_permission_error(self) -> None:
        session = mock.Mock()
        session.post.return_value = _response(401, {"detail": "Invalid or revoked API key."})

        with pytest.raises(RedataPermissionError):
            self._gateway(session).resolve_cids([1])

    def test_batches_are_chunked_at_the_documented_cap(self) -> None:
        session = mock.Mock()
        session.post.return_value = _response(200, {"results": {}, "pending": []})

        with mock.patch.object(gw_module, "_MAX_CIDS_PER_REQUEST", 2):
            self._gateway(session).resolve_cids([1, 2, 3, 4, 5])

        # 5 cids at a cap of 2 -> 3 requests (2 + 2 + 1).
        self.assertEqual(session.post.call_count, 3)
        sent_batches = [call.kwargs["json"]["cids"] for call in session.post.call_args_list]
        self.assertEqual(sent_batches, [[1, 2], [3, 4], [5]])

    def test_empty_input_makes_no_request(self) -> None:
        session = mock.Mock()

        result = self._gateway(session).resolve_cids([])

        session.post.assert_not_called()
        self.assertEqual(result.resolved, {})
        self.assertEqual(result.unresolvable, set())
        self.assertEqual(result.pending, set())

    def test_entry_with_url_is_sent_as_a_cid_url_object(self) -> None:
        """REData resolves faster/more reliably from a place's own URL than cid alone."""
        session = mock.Mock()
        session.post.return_value = _response(200, {"results": {}, "pending": []})

        self._gateway(session).resolve_cids(
            [CidLookupEntry(cid=1, url="https://maps.google.com/maps/place/X/data=!4m2!3m1!1s0x0:0x1")],
        )

        sent = session.post.call_args.kwargs["json"]["cids"]
        self.assertEqual(sent, [{"cid": 1, "url": "https://maps.google.com/maps/place/X/data=!4m2!3m1!1s0x0:0x1"}])

    def test_mixed_plain_and_url_entries_are_sent_in_their_own_shape(self) -> None:
        session = mock.Mock()
        session.post.return_value = _response(200, {"results": {}, "pending": []})

        self._gateway(session).resolve_cids([1, CidLookupEntry(cid=2, url="https://maps.google.com/maps/place/Y")])

        sent = session.post.call_args.kwargs["json"]["cids"]
        self.assertEqual(sent, [1, {"cid": 2, "url": "https://maps.google.com/maps/place/Y"}])

    def test_a_cid_url_entry_still_resolves_keyed_by_its_own_cid(self) -> None:
        session = mock.Mock()
        session.post.return_value = _response(200, {"results": {"1": {"lat": 38.456, "lng": -77.123}}, "pending": []})

        result = self._gateway(session).resolve_cids([CidLookupEntry(cid=1, url="https://maps.google.com/maps/place/X")])

        self.assertEqual(result.resolved, {1: (38.456, -77.123)})
