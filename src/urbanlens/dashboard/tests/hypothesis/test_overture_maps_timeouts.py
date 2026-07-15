"""Regression guard for OvertureMapsGateway's default S3 read timeouts.

Unbounded connect_timeout/request_timeout (the old default: both None) let a
stalled S3 range-read tie up a Celery worker indefinitely, since pyarrow's
read isn't interruptible the way a plain `requests` call is - reported as the
"Building Characteristics" pin-detail panel getting stuck in a loading state
(and, by tying up worker capacity, delaying other panels queued behind it).
"""

from __future__ import annotations

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.apis.locations.boundaries.overture_maps import OvertureMapsGateway


class OvertureMapsGatewayTimeoutDefaultsTests(TestCase):
    def test_connect_timeout_defaults_to_a_bound_not_none(self) -> None:
        gateway = OvertureMapsGateway()
        self.assertIsNotNone(gateway.connect_timeout)
        self.assertGreater(gateway.connect_timeout, 0)

    def test_request_timeout_defaults_to_a_bound_not_none(self) -> None:
        gateway = OvertureMapsGateway()
        self.assertIsNotNone(gateway.request_timeout)
        self.assertGreater(gateway.request_timeout, 0)

    def test_timeouts_can_still_be_overridden_explicitly(self) -> None:
        gateway = OvertureMapsGateway(connect_timeout=5, request_timeout=15)
        self.assertEqual(gateway.connect_timeout, 5)
        self.assertEqual(gateway.request_timeout, 15)

    def test_none_can_still_be_passed_explicitly_to_opt_out(self) -> None:
        gateway = OvertureMapsGateway(connect_timeout=None, request_timeout=None)
        self.assertIsNone(gateway.connect_timeout)
        self.assertIsNone(gateway.request_timeout)
