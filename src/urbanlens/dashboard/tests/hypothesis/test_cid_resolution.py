"""Tests for services.apis.locations.cid_resolution.resolve_cids's provider dispatch.

Covers the REData-vs-Google-Places choice and how each provider's failure
modes map onto CidResolutionResult's resolved/unresolvable/pending buckets -
see cid_resolution.py's module docstring for why each bucket exists.
"""

from __future__ import annotations

from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations import cid_resolution
from urbanlens.dashboard.services.gateway import GatewayRequestError
from urbanlens.dashboard.services.rate_limiter import RateLimitExceededError
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
            gateway_cls.return_value.resolve_cids.return_value = {1: (1.0, 2.0)}
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
        google_cls.assert_not_called()

    def test_explicit_null_is_unresolvable_not_pending(self) -> None:
        with mock.patch(
            "urbanlens.dashboard.services.apis.locations.cid_resolution.RedataCidGateway",
        ) as gateway_cls:
            gateway_cls.return_value.resolve_cids.return_value = {1: (1.0, 2.0), 2: None}
            result = cid_resolution.resolve_cids([1, 2])

        self.assertEqual(result.resolved, {1: (1.0, 2.0)})
        self.assertEqual(result.unresolvable, {2})
        self.assertEqual(result.pending, [])


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
