"""Tier classification for the external API's throttles.

The tier is derived from a view's own ``required_scopes_by_method``, so these
tests pin down the derivation rather than the rates themselves: a
misclassification would either strangle a legitimate sync client (reads counted
as writes) or, worse, let a runaway write loop spend the far more generous read
budget.

Pure logic, so no database is needed.
"""

from __future__ import annotations

from django.conf import settings
from django.test import SimpleTestCase
from hypothesis import given
from hypothesis import strategies as st

from urbanlens.dashboard.external_api.throttling import (
    TIER_READ,
    TIER_WRITE,
    ExternalApiBurstThrottle,
    ExternalApiReadThrottle,
    ExternalApiWriteThrottle,
    request_tier,
)
from urbanlens.dashboard.models.account.model import ApiKeyScope


class _FakeView:
    """A view declaring scopes per method, optionally with an explicit tier override."""

    def __init__(self, required_scopes_by_method: dict | None = None, throttle_tier_by_method: dict | None = None) -> None:
        if required_scopes_by_method is not None:
            self.required_scopes_by_method = required_scopes_by_method
        if throttle_tier_by_method is not None:
            self.throttle_tier_by_method = throttle_tier_by_method


class RequestTierTests(SimpleTestCase):
    """request_tier classifies by declared scopes, failing conservative."""

    def test_read_only_get_is_read_tier(self) -> None:
        """The ordinary case: a GET requiring one :read scope."""
        view = _FakeView({"GET": frozenset({ApiKeyScope.PINS_READ})})
        self.assertEqual(request_tier(view, "GET"), TIER_READ)

    def test_post_requiring_only_read_scopes_is_read_tier(self) -> None:
        """Classification follows the scopes, not the HTTP verb.

        A search endpoint is a POST (its query is too big for a query string)
        yet requires only ``search:read`` - it costs what a read costs.
        """
        view = _FakeView({"POST": frozenset({ApiKeyScope.SEARCH_READ})})
        self.assertEqual(request_tier(view, "POST"), TIER_READ)

    def test_write_scope_is_write_tier(self) -> None:
        """A :write scope makes the request a write."""
        view = _FakeView({"POST": frozenset({ApiKeyScope.PINS_WRITE})})
        self.assertEqual(request_tier(view, "POST"), TIER_WRITE)

    def test_manage_scope_is_write_tier(self) -> None:
        """:manage mutates too - push:manage registers and removes devices."""
        view = _FakeView({"POST": frozenset({ApiKeyScope.PUSH_MANAGE})})
        self.assertEqual(request_tier(view, "POST"), TIER_WRITE)

    def test_mixed_scopes_are_write_tier(self) -> None:
        """One write scope among reads is enough to make the whole request a write."""
        view = _FakeView({"POST": frozenset({ApiKeyScope.PINS_READ, ApiKeyScope.PINS_WRITE})})
        self.assertEqual(request_tier(view, "POST"), TIER_WRITE)

    def test_missing_method_declaration_is_write_tier(self) -> None:
        """An undeclared method falls into the tighter bucket, not the looser one."""
        view = _FakeView({"GET": frozenset({ApiKeyScope.PINS_READ})})
        self.assertEqual(request_tier(view, "DELETE"), TIER_WRITE)

    def test_empty_declaration_is_write_tier(self) -> None:
        """An empty scope set is treated as undeclared, so also a write."""
        view = _FakeView({"GET": frozenset()})
        self.assertEqual(request_tier(view, "GET"), TIER_WRITE)

    def test_view_without_declaration_attribute_is_write_tier(self) -> None:
        """A view missing the attribute entirely still classifies conservatively."""
        self.assertEqual(request_tier(_FakeView(), "GET"), TIER_WRITE)

    def test_explicit_override_wins_over_scopes(self) -> None:
        """throttle_tier_by_method beats what the scopes would imply."""
        view = _FakeView({"GET": frozenset()}, {"GET": TIER_READ})
        self.assertEqual(request_tier(view, "GET"), TIER_READ)

    def test_explicit_override_can_tighten(self) -> None:
        """The override works in the strict direction too, for costly reads."""
        view = _FakeView({"GET": frozenset({ApiKeyScope.PINS_READ})}, {"GET": TIER_WRITE})
        self.assertEqual(request_tier(view, "GET"), TIER_WRITE)

    @given(
        read_scopes=st.lists(st.sampled_from([s for s in ApiKeyScope if s.value.endswith(":read")]), min_size=1, max_size=4),
        method=st.sampled_from(["GET", "POST", "PATCH", "DELETE"]),
    )
    def test_any_all_read_declaration_is_read_tier(self, read_scopes: list, method: str) -> None:
        """Any combination of purely-:read scopes classifies as a read, on any verb."""
        view = _FakeView({method: frozenset(read_scopes)})
        self.assertEqual(request_tier(view, method), TIER_READ)

    @given(
        scopes=st.lists(st.sampled_from(list(ApiKeyScope)), min_size=1, max_size=5),
        method=st.sampled_from(["GET", "POST", "PATCH", "DELETE"]),
    )
    def test_declaration_containing_a_mutating_scope_is_write_tier(self, scopes: list, method: str) -> None:
        """Whenever a :write/:manage scope is present, the tier is write."""
        view = _FakeView({method: frozenset(scopes)})
        expected = TIER_WRITE if any(s.value.endswith((":write", ":manage")) for s in scopes) else TIER_READ
        self.assertEqual(request_tier(view, method), expected)


class ThrottleConfigurationTests(SimpleTestCase):
    """The three throttle classes are wired to configured rates.

    ``SimpleRateThrottle`` resolves its rate in ``__init__`` and raises
    ``ImproperlyConfigured`` on a missing key, so merely constructing each class
    proves the settings entry exists - which is why the settings change and the
    class change had to ship together.
    """

    def test_read_throttle_constructs_with_its_rate(self) -> None:
        """external_api_read is configured and bound to the read tier."""
        throttle = ExternalApiReadThrottle()
        self.assertEqual(throttle.tier, TIER_READ)
        self.assertIsNotNone(throttle.rate)

    def test_write_throttle_constructs_with_its_rate(self) -> None:
        """external_api_write is configured and bound to the write tier."""
        throttle = ExternalApiWriteThrottle()
        self.assertEqual(throttle.tier, TIER_WRITE)
        self.assertIsNotNone(throttle.rate)

    def test_burst_throttle_applies_to_every_tier(self) -> None:
        """The burst throttle declares no tier, so it counts all requests."""
        throttle = ExternalApiBurstThrottle()
        self.assertEqual(throttle.tier, "")
        self.assertIsNotNone(throttle.rate)

    def test_retired_flat_scope_is_gone(self) -> None:
        """The old single external_api_key rate no longer exists."""
        self.assertNotIn("external_api_key", settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"])

    def test_writes_are_capped_tighter_than_reads(self) -> None:
        """The whole point of the split: writes get the smaller hourly budget."""
        rates = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]
        read_count = int(rates["external_api_read"].split("/")[0])
        write_count = int(rates["external_api_write"].split("/")[0])
        self.assertLess(write_count, read_count)


class ThrottleTierSkippingTests(SimpleTestCase):
    """A tiered throttle waves through requests belonging to the other tier."""

    class _Request:
        def __init__(self, method: str) -> None:
            self.method = method
            self.auth = None

    def test_read_throttle_skips_a_write_request(self) -> None:
        """A write request is not counted against the read budget."""
        view = _FakeView({"POST": frozenset({ApiKeyScope.PINS_WRITE})})
        self.assertTrue(ExternalApiReadThrottle().allow_request(self._Request("POST"), view))

    def test_write_throttle_skips_a_read_request(self) -> None:
        """A read request is not counted against the write budget."""
        view = _FakeView({"GET": frozenset({ApiKeyScope.PINS_READ})})
        self.assertTrue(ExternalApiWriteThrottle().allow_request(self._Request("GET"), view))

    def test_unauthenticated_request_is_not_throttled(self) -> None:
        """No credential means no cache key, which DRF treats as "don't throttle"."""
        view = _FakeView({"GET": frozenset({ApiKeyScope.PINS_READ})})
        self.assertIsNone(ExternalApiReadThrottle().get_cache_key(self._Request("GET"), view))
