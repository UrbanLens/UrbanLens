"""P110: Overture's parquet/STAC reads must be visible to, and bounded by, the rate limiter.

`_fetch` bypasses `self.session` entirely - it reads GeoParquet straight from S3 via
`pyarrow`/`geopandas` via the `overturemaps` library, so `Gateway.__post_init__`'s automatic
session-wrapping never sees these calls (`OvertureMapsGateway.service_key` is set to `None` on the
class precisely so nothing tries). Nothing bounded how often enrichment reached Overture in the
first place, including the STAC index lookup that actually earns Overture's 429 - see P110's "Still
open" note: "the reads remain invisible to the rate limiter and to the outbound-call guard."

This tests the fix: `OvertureMapsGateway._reserve_call_budget`, called at the top of `_fetch`
before the STAC lookup or the data read.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.api_call_log import ApiCallLog
from urbanlens.dashboard.models.api_rate_limit import ApiRateLimit
from urbanlens.dashboard.services.apis.locations.boundaries.overture_maps import OvertureMapsGateway
from urbanlens.dashboard.services.core.gateway import GatewayRateLimitedError

#: A bbox the size every caller here actually uses - a single building.
SMALL_BBOX = (-71.059, 42.36, -71.058, 42.361)

_SERVICE = "overture_maps"
_GEODATAFRAME = "urbanlens.dashboard.services.apis.locations.boundaries.overture_maps._read_files"
_STAC_LOOKUP = "urbanlens.dashboard.services.apis.locations.boundaries.overture_maps._intersecting_files"


def _reset_breaker() -> None:
    """Close the STAC circuit, so one test's refusal does not silence the next."""
    from urbanlens.dashboard.services.apis.locations.boundaries import overture_maps

    overture_maps._stac_unavailable_until = 0.0  # noqa: SLF001
    overture_maps._latest_release_cache = None  # noqa: SLF001


class OvertureCallBudgetTests(TestCase):
    """A tight, explicit budget so a refusal is reachable in a handful of calls."""

    def setUp(self) -> None:
        super().setUp()
        ApiRateLimit.objects.update_or_create(
            service=_SERVICE,
            defaults={"calls_per_minute": 2, "calls_per_day": None, "calls_per_30_days": None, "enabled": True},
        )
        _reset_breaker()
        latest = patch("overturemaps.core.get_latest_release", return_value="2026-09-17.0")
        latest.start()
        self.addCleanup(latest.stop)
        self.addCleanup(_reset_breaker)

    def test_a_call_within_budget_reaches_overture(self) -> None:
        gateway = OvertureMapsGateway()
        with patch(_STAC_LOOKUP, return_value=["bucket/one.parquet"]), patch(_GEODATAFRAME) as geodataframe:
            gateway.get_buildings(SMALL_BBOX)

        geodataframe.assert_called_once()

    def test_a_call_within_budget_is_logged_as_a_successful_overture_maps_call(self) -> None:
        """The prior gap: these calls were invisible to `ApiCallLog` entirely."""
        gateway = OvertureMapsGateway()
        with patch(_STAC_LOOKUP, return_value=["bucket/one.parquet"]), patch(_GEODATAFRAME):
            gateway.get_buildings(SMALL_BBOX)

        entry = ApiCallLog.objects.for_service(_SERVICE).latest("created")
        self.assertTrue(entry.success)
        self.assertEqual(entry.endpoint, "building")

    def test_exceeding_the_budget_refuses_before_touching_the_stac_index(self) -> None:
        """The gate sits ahead of `_require_narrowing` - a spent budget must never probe the index."""
        gateway = OvertureMapsGateway()
        with patch(_STAC_LOOKUP, return_value=["bucket/one.parquet"]), patch(_GEODATAFRAME) as geodataframe:
            gateway.get_buildings(SMALL_BBOX)
            gateway.get_buildings(SMALL_BBOX)

        with patch(_STAC_LOOKUP) as stac_lookup, pytest.raises(GatewayRateLimitedError):
            gateway.get_buildings(SMALL_BBOX)

        stac_lookup.assert_not_called()
        self.assertEqual(geodataframe.call_count, 2, "the third call reached the data read too, past its own budget")

    def test_a_refused_call_is_logged_as_rate_limited(self) -> None:
        gateway = OvertureMapsGateway()
        with patch(_STAC_LOOKUP, return_value=["bucket/one.parquet"]), patch(_GEODATAFRAME):
            gateway.get_buildings(SMALL_BBOX)
            gateway.get_buildings(SMALL_BBOX)
            with pytest.raises(GatewayRateLimitedError):
                gateway.get_buildings(SMALL_BBOX)

        refused = ApiCallLog.objects.for_service(_SERVICE).rate_limited()
        self.assertEqual(refused.count(), 1)

    def test_a_failed_read_is_logged_as_unsuccessful_not_swallowed(self) -> None:
        """The gate must not turn a real Overture/pyarrow failure into a silent success row."""
        gateway = OvertureMapsGateway()
        with (
            patch(_STAC_LOOKUP, return_value=["bucket/one.parquet"]),
            patch(_GEODATAFRAME, side_effect=OSError("boom")),
            pytest.raises(OSError, match="boom"),
        ):
            gateway.get_buildings(SMALL_BBOX)

        entry = ApiCallLog.objects.for_service(_SERVICE).latest("created")
        self.assertFalse(entry.success)

    def test_a_disabled_service_refuses_without_touching_overture_at_all(self) -> None:
        """Closes the other half of P110's gap: the outbound-call guard, not just the rate window."""
        ApiRateLimit.objects.filter(service=_SERVICE).update(enabled=False)
        gateway = OvertureMapsGateway()
        with (
            patch(_STAC_LOOKUP) as stac_lookup,
            patch(_GEODATAFRAME) as geodataframe,
            pytest.raises(GatewayRateLimitedError),
        ):
            gateway.get_buildings(SMALL_BBOX)

        stac_lookup.assert_not_called()
        geodataframe.assert_not_called()

    def test_an_enabled_service_at_default_settings_still_proceeds(self) -> None:
        """The contrast, so the disabled-service test above is measuring that flag and nothing else."""
        gateway = OvertureMapsGateway()
        with patch(_STAC_LOOKUP, return_value=["bucket/one.parquet"]), patch(_GEODATAFRAME) as geodataframe:
            gateway.get_buildings(SMALL_BBOX)

        geodataframe.assert_called_once()
