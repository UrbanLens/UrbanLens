"""Tests for services.apis.locations.cid_resolution.resolve_cids's provider dispatch."""

from __future__ import annotations

from unittest import mock

import requests

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.plugins.points import CID_RESOLVERS
from urbanlens.dashboard.services.apis.locations import cid_resolution
from urbanlens.dashboard.services.apis.locations.cid_resolution import (
    CidResolutionResult,
    CidResolver,
    GoogleCidResolver,
)
from urbanlens.dashboard.services.core.rate_limiter import RateLimitExceededError


class _FakeResolver(CidResolver):
    """Answers with ``result`` when ``is_available``, remembering every call."""

    def __init__(self, key: str, result: CidResolutionResult | None = None, *, is_available: bool = True) -> None:
        self.key = key
        self.is_available = is_available
        self.result = result or CidResolutionResult(provider=key)
        self.calls: list[tuple[list[int], dict[int, str] | None]] = []

    def available(self) -> bool:
        return self.is_available

    def resolve(self, cids: list[int], urls_by_cid: dict[int, str] | None = None) -> CidResolutionResult:
        self.calls.append((list(cids), urls_by_cid))
        return self.result


class ResolveCidsProviderChoiceTests(SimpleTestCase):
    def test_the_first_available_resolver_answers(self) -> None:
        first = _FakeResolver("first", CidResolutionResult(provider="first", resolved={1: (1.0, 2.0)}))
        second = _FakeResolver("second")
        with CID_RESOLVERS.overridden([first, second]):
            result = cid_resolution.resolve_cids([1])

        self.assertEqual(result.provider, "first")
        self.assertEqual(result.resolved, {1: (1.0, 2.0)})
        self.assertEqual(second.calls, [])

    def test_an_unavailable_resolver_is_skipped(self) -> None:
        skipped = _FakeResolver("skipped", is_available=False)
        used = _FakeResolver("used", CidResolutionResult(provider="used", resolved={1: (1.0, 2.0)}))
        with CID_RESOLVERS.overridden([skipped, used]):
            result = cid_resolution.resolve_cids([1])

        self.assertEqual(result.provider, "used")
        self.assertEqual(skipped.calls, [])

    def test_a_failed_batch_is_not_handed_to_the_next_resolver(self) -> None:
        """An unreachable resolver must never silently fall back to a costlier one."""
        failing = _FakeResolver("failing", CidResolutionResult(provider="failing", pending=[1, 2], request_failed=True))
        fallback = _FakeResolver("fallback")
        with CID_RESOLVERS.overridden([failing, fallback]):
            result = cid_resolution.resolve_cids([1, 2])

        self.assertEqual(result.pending, [1, 2])
        self.assertTrue(result.request_failed)
        self.assertEqual(fallback.calls, [])

    def test_no_available_resolver_defers_the_batch_as_a_failed_request(self) -> None:
        with CID_RESOLVERS.overridden([_FakeResolver("off", is_available=False)]):
            result = cid_resolution.resolve_cids([1, 2])

        self.assertEqual(result.pending, [1, 2])
        self.assertTrue(result.request_failed)
        self.assertEqual(result.resolved, {})

    def test_urls_by_cid_are_passed_to_the_resolver(self) -> None:
        resolver = _FakeResolver("fake")
        urls = {1: "https://maps.google.com/maps/place/X"}
        with CID_RESOLVERS.overridden([resolver]):
            cid_resolution.resolve_cids([1, 2], urls_by_cid=urls)

        self.assertEqual(resolver.calls, [([1, 2], urls)])


class ResolveViaGoogleTests(SimpleTestCase):
    def test_rate_limit_stops_the_batch_and_defers_the_remainder(self) -> None:
        with (
            CID_RESOLVERS.overridden([GoogleCidResolver()]),
            mock.patch(
                "urbanlens.dashboard.services.apis.locations.cid_resolution.GoogleGeocodingGateway"
            ) as gateway_cls,
        ):
            gateway = gateway_cls.return_value
            gateway.get_coordinates_by_cid.side_effect = [(1.0, 2.0), RateLimitExceededError("google_geocoding")]
            result = cid_resolution.resolve_cids([1, 2, 3])

        self.assertEqual(result.provider, cid_resolution.PROVIDER_GOOGLE)
        self.assertEqual(result.resolved, {1: (1.0, 2.0)})
        # cid 2 triggered the limit; cid 3 was never attempted - both deferred.
        self.assertEqual(result.pending, [2, 3])

    def test_not_found_status_is_unresolvable_not_pending(self) -> None:
        with (
            CID_RESOLVERS.overridden([GoogleCidResolver()]),
            mock.patch(
                "urbanlens.dashboard.services.apis.locations.cid_resolution.GoogleGeocodingGateway"
            ) as gateway_cls,
        ):
            gateway_cls.return_value.get_coordinates_by_cid.return_value = (None, None)
            result = cid_resolution.resolve_cids([1])

        self.assertEqual(result.unresolvable, {1})
        self.assertEqual(result.pending, [])

    def test_transient_request_error_defers_only_that_cid_and_keeps_going(self) -> None:
        """Unlike a rate limit, one cid's transient failure must not abort the rest of the batch."""
        with (
            CID_RESOLVERS.overridden([GoogleCidResolver()]),
            mock.patch(
                "urbanlens.dashboard.services.apis.locations.cid_resolution.GoogleGeocodingGateway"
            ) as gateway_cls,
        ):
            gateway = gateway_cls.return_value
            gateway.get_coordinates_by_cid.side_effect = [requests.ConnectionError("boom"), (3.0, 4.0)]
            result = cid_resolution.resolve_cids([1, 2])

        self.assertEqual(result.pending, [1])
        self.assertEqual(result.resolved, {2: (3.0, 4.0)})
        self.assertEqual(result.unresolvable, set())


class ResolveCidsRefusesBeforeAnyLookupTests(SimpleTestCase):
    """A cid that cannot name a place costs no lookup from any resolver."""

    ROUNDED = 14522379626423718000  # 0xc989db53ce5b1234 after a JavaScript Number.
    TRUE = 0xC989DB53CE5B1234

    def test_a_float_rounded_cid_never_reaches_a_resolver(self) -> None:
        resolver = _FakeResolver("fake", CidResolutionResult(provider="fake", resolved={7: (1.0, 2.0)}))
        with CID_RESOLVERS.overridden([resolver]):
            result = cid_resolution.resolve_cids([7, self.ROUNDED])

        self.assertEqual([cids for cids, _urls in resolver.calls], [[7]])
        self.assertEqual(result.rejected, {self.ROUNDED: "cid_float_rounded"})
        self.assertEqual(result.resolved, {7: (1.0, 2.0)})

    def test_a_float_rounded_cid_never_reaches_google_places(self) -> None:
        with (
            CID_RESOLVERS.overridden([GoogleCidResolver()]),
            mock.patch(
                "urbanlens.dashboard.services.apis.locations.cid_resolution.GoogleGeocodingGateway"
            ) as gateway_cls,
        ):
            result = cid_resolution.resolve_cids([self.ROUNDED])

        gateway_cls.return_value.get_coordinates_by_cid.assert_not_called()
        self.assertEqual(result.rejected, {self.ROUNDED: "cid_float_rounded"})
        self.assertEqual(result.pending, [])

    def test_a_cid_its_own_url_would_replace_is_refused_so_no_answer_goes_astray(self) -> None:
        url = f"https://www.google.com/maps/place/X/data=!4m2!3m1!1s0x1:0x{self.TRUE:x}"
        resolver = _FakeResolver("fake")
        with CID_RESOLVERS.overridden([resolver]):
            result = cid_resolution.resolve_cids([self.ROUNDED], urls_by_cid={self.ROUNDED: url})

        self.assertEqual([cids for cids, _urls in resolver.calls], [[]])
        self.assertEqual(result.rejected, {self.ROUNDED: "cid_float_rounded"})

    def test_a_resolvers_own_refusals_are_passed_through(self) -> None:
        resolver = _FakeResolver("fake", CidResolutionResult(provider="fake", rejected={7: "cid_conflicts_with_url"}))
        with CID_RESOLVERS.overridden([resolver]):
            result = cid_resolution.resolve_cids([7])

        self.assertEqual(result.rejected, {7: "cid_conflicts_with_url"})
