"""Tests for services.apis.locations.cid_resolution.resolve_cids's provider dispatch.

Covers the REData-vs-Google-Places choice and how each provider's failure
modes map onto CidResolutionResult's resolved/unresolvable/pending buckets -
see cid_resolution.py's module docstring for why each bucket exists.
"""

from __future__ import annotations

from unittest import mock

import requests

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations import cid_resolution
from urbanlens.dashboard.services.apis.locations.google.redata_cid_gateway import CidLookupEntry, RedataCidBatchResult, RedataPermissionError
from urbanlens.dashboard.services.core.gateway import GatewayRequestError
from urbanlens.dashboard.services.core.rate_limiter import RateLimitExceededError
from urbanlens.UrbanLens.settings.app import settings


class ResolveCidsProviderChoiceTests(SimpleTestCase):
    def test_redata_configured_uses_redata(self) -> None:
        with (
            mock.patch.object(settings, "redata_api_url", "https://redata.example.test"),
            mock.patch.object(settings, "redata_api_key", "test-key"),
            mock.patch(
                "urbanlens.dashboard.services.apis.locations.cid_resolution.RedataCidGateway",
            ) as gateway_cls,
        ):
            gateway_cls.return_value.resolve_cids.return_value = RedataCidBatchResult(resolved={1: (1.0, 2.0)})
            result = cid_resolution.resolve_cids([1])

        self.assertEqual(result.provider, cid_resolution.PROVIDER_REDATA)
        self.assertEqual(result.resolved, {1: (1.0, 2.0)})

    def test_redata_not_configured_uses_google(self) -> None:
        with (
            mock.patch.object(settings, "redata_api_url", None),
            mock.patch.object(settings, "redata_api_key", None),
            mock.patch(
                "urbanlens.dashboard.services.apis.locations.cid_resolution.GoogleGeocodingGateway",
            ) as gateway_cls,
        ):
            gateway_cls.return_value.get_coordinates_by_cid.return_value = (1.0, 2.0)
            result = cid_resolution.resolve_cids([1])

        self.assertEqual(result.provider, cid_resolution.PROVIDER_GOOGLE)
        self.assertEqual(result.resolved, {1: (1.0, 2.0)})

    def test_redata_url_without_key_uses_google(self) -> None:
        """Both settings are required - a URL alone must not be treated as configured."""
        with (
            mock.patch.object(settings, "redata_api_url", "https://redata.example.test"),
            mock.patch.object(settings, "redata_api_key", None),
            mock.patch(
                "urbanlens.dashboard.services.apis.locations.cid_resolution.GoogleGeocodingGateway",
            ) as gateway_cls,
            mock.patch(
                "urbanlens.dashboard.services.apis.locations.cid_resolution.RedataCidGateway",
            ) as redata_cls,
        ):
            gateway_cls.return_value.get_coordinates_by_cid.return_value = (1.0, 2.0)
            result = cid_resolution.resolve_cids([1])

        self.assertEqual(result.provider, cid_resolution.PROVIDER_GOOGLE)
        redata_cls.assert_not_called()

    def test_redata_key_without_url_uses_google(self) -> None:
        """Both settings are required - a key alone must not be treated as configured."""
        with (
            mock.patch.object(settings, "redata_api_url", None),
            mock.patch.object(settings, "redata_api_key", "test-key"),
            mock.patch(
                "urbanlens.dashboard.services.apis.locations.cid_resolution.GoogleGeocodingGateway",
            ) as gateway_cls,
            mock.patch(
                "urbanlens.dashboard.services.apis.locations.cid_resolution.RedataCidGateway",
            ) as redata_cls,
        ):
            gateway_cls.return_value.get_coordinates_by_cid.return_value = (1.0, 2.0)
            result = cid_resolution.resolve_cids([1])

        self.assertEqual(result.provider, cid_resolution.PROVIDER_GOOGLE)
        redata_cls.assert_not_called()


class ResolveViaRedataTests(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._patchers = [
            mock.patch.object(settings, "redata_api_url", "https://redata.example.test"),
            mock.patch.object(settings, "redata_api_key", "test-key"),
        ]
        for p in self._patchers:
            p.start()
            self.addCleanup(p.stop)

    def test_gateway_failure_defers_the_whole_batch_no_fallback_to_google(self) -> None:
        """REData being unreachable must never silently fall back to Google."""
        with (
            mock.patch(
                "urbanlens.dashboard.services.apis.locations.cid_resolution.RedataCidGateway",
            ) as redata_cls,
            mock.patch(
                "urbanlens.dashboard.services.apis.locations.cid_resolution.GoogleGeocodingGateway",
            ) as google_cls,
        ):
            redata_cls.return_value.resolve_cids.side_effect = GatewayRequestError("boom")
            result = cid_resolution.resolve_cids([1, 2])

        self.assertEqual(result.pending, [1, 2])
        self.assertEqual(result.resolved, {})
        self.assertFalse(result.auth_failed)
        # Distinguishes "the whole batch made zero progress this attempt" from a
        # normal response that resolved/deferred cids - see tasks.py's
        # consecutive-failure cap, which uses this to eventually give up on a
        # persistently unreachable REData instead of retrying forever.
        self.assertTrue(result.request_failed)
        google_cls.assert_not_called()

    def test_permission_error_marks_auth_failed_and_does_not_fall_back_to_google(self) -> None:
        """A rejected API key will never succeed by retrying - callers must be told to stop."""
        with (
            mock.patch(
                "urbanlens.dashboard.services.apis.locations.cid_resolution.RedataCidGateway",
            ) as redata_cls,
            mock.patch(
                "urbanlens.dashboard.services.apis.locations.cid_resolution.GoogleGeocodingGateway",
            ) as google_cls,
        ):
            redata_cls.return_value.resolve_cids.side_effect = RedataPermissionError("boom")
            result = cid_resolution.resolve_cids([1, 2])

        self.assertTrue(result.auth_failed)
        # auth_failed is already terminal on its own (the task stops on the
        # first occurrence) - request_failed is irrelevant here, but should
        # still read False rather than conflating the two failure kinds.
        self.assertFalse(result.request_failed)
        self.assertEqual(result.pending, [1, 2])
        self.assertEqual(result.resolved, {})
        google_cls.assert_not_called()

    def test_explicit_null_is_unresolvable_not_pending(self) -> None:
        with mock.patch(
            "urbanlens.dashboard.services.apis.locations.cid_resolution.RedataCidGateway",
        ) as gateway_cls:
            gateway_cls.return_value.resolve_cids.return_value = RedataCidBatchResult(
                resolved={1: (1.0, 2.0)},
                unresolvable={2},
            )
            result = cid_resolution.resolve_cids([1, 2])

        self.assertEqual(result.resolved, {1: (1.0, 2.0)})
        self.assertEqual(result.unresolvable, {2})
        self.assertEqual(result.pending, [])

    def test_urls_by_cid_is_forwarded_as_cid_lookup_entries(self) -> None:
        """A cid with a known source Google Maps URL resolves faster/more reliably at
        REData when the URL is sent alongside it - see RedataCidGateway/CidLookupEntry."""
        with mock.patch(
            "urbanlens.dashboard.services.apis.locations.cid_resolution.RedataCidGateway",
        ) as gateway_cls:
            gateway_cls.return_value.resolve_cids.return_value = RedataCidBatchResult()
            cid_resolution.resolve_cids([1, 2], urls_by_cid={1: "https://maps.google.com/maps/place/X"})

        sent_entries = gateway_cls.return_value.resolve_cids.call_args.args[0]
        self.assertEqual(sent_entries, [CidLookupEntry(cid=1, url="https://maps.google.com/maps/place/X"), CidLookupEntry(cid=2, url=None)])

    def test_still_pending_on_redatas_end_is_reported_as_pending(self) -> None:
        with mock.patch(
            "urbanlens.dashboard.services.apis.locations.cid_resolution.RedataCidGateway",
        ) as gateway_cls:
            gateway_cls.return_value.resolve_cids.return_value = RedataCidBatchResult(pending={3})
            result = cid_resolution.resolve_cids([3])

        self.assertEqual(result.resolved, {})
        self.assertEqual(result.unresolvable, set())
        self.assertEqual(result.pending, [3])
        # REData responding successfully (even with cids still pending on its
        # own end) is real progress, unlike the request itself failing - must
        # not count toward the consecutive-failure cap.
        self.assertFalse(result.request_failed)


class ResolveViaGoogleTests(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._patchers = [
            mock.patch.object(settings, "redata_api_url", None),
            mock.patch.object(settings, "redata_api_key", None),
        ]
        for p in self._patchers:
            p.start()
            self.addCleanup(p.stop)

    def test_rate_limit_stops_the_batch_and_defers_the_remainder(self) -> None:
        with mock.patch(
            "urbanlens.dashboard.services.apis.locations.cid_resolution.GoogleGeocodingGateway",
        ) as gateway_cls:
            gateway = gateway_cls.return_value
            gateway.get_coordinates_by_cid.side_effect = [(1.0, 2.0), RateLimitExceededError("google_geocoding")]
            result = cid_resolution.resolve_cids([1, 2, 3])

        self.assertEqual(result.resolved, {1: (1.0, 2.0)})
        # cid 2 triggered the limit; cid 3 was never attempted - both deferred.
        self.assertEqual(result.pending, [2, 3])

    def test_not_found_status_is_unresolvable_not_pending(self) -> None:
        with mock.patch(
            "urbanlens.dashboard.services.apis.locations.cid_resolution.GoogleGeocodingGateway",
        ) as gateway_cls:
            gateway_cls.return_value.get_coordinates_by_cid.return_value = (None, None)
            result = cid_resolution.resolve_cids([1])

        self.assertEqual(result.unresolvable, {1})
        self.assertEqual(result.pending, [])

    def test_transient_request_error_defers_only_that_cid_and_keeps_going(self) -> None:
        """Unlike a rate limit, one cid's transient failure must not abort the rest of the batch."""
        with mock.patch(
            "urbanlens.dashboard.services.apis.locations.cid_resolution.GoogleGeocodingGateway",
        ) as gateway_cls:
            gateway = gateway_cls.return_value
            gateway.get_coordinates_by_cid.side_effect = [requests.ConnectionError("boom"), (3.0, 4.0)]
            result = cid_resolution.resolve_cids([1, 2])

        self.assertEqual(result.pending, [1])
        self.assertEqual(result.resolved, {2: (3.0, 4.0)})
        self.assertEqual(result.unresolvable, set())
