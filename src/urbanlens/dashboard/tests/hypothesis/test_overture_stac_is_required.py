"""P110: `stac=True` is a request, and the library grants it only when it can."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations.boundaries.overture_maps import OvertureMapsGateway
from urbanlens.dashboard.services.core.gateway import GatewayRateLimitedError

#: A bbox the size every caller here actually uses - a single building.
SMALL_BBOX = (-71.059, 42.36, -71.058, 42.361)

_GEODATAFRAME = "urbanlens.dashboard.services.apis.locations.boundaries.overture_maps._overture_geodataframe"
_STAC_LOOKUP = "overturemaps.core._get_files_from_stac"
_LATEST_RELEASE = "overturemaps.core.get_latest_release"


class TheLibraryFallsBackToThePlanetTests(SimpleTestCase):
    """Pinning the third-party behaviour this depends on, so an upgrade is visible.

    Not a test of our code."""

    def test_a_failed_stac_lookup_returns_none_rather_than_raising(self) -> None:
        from overturemaps import core

        with patch.object(core, "urlopen", side_effect=OSError("HTTP Error 429: Too Many Requests")):
            self.assertIsNone(core._get_files_from_stac("buildings", "building", _bbox(), "2026-08-19.0"))  # noqa: SLF001

    def test_the_caller_opens_the_whole_theme_when_the_lookup_failed(self) -> None:
        """The behaviour, not the source text: what does it hand to pyarrow?

        With a narrowed lookup it passes a *list* of intersecting S3 keys. With
        a failed one it passes the theme's *path*, which is the entire release.
        """
        from overturemaps import core

        with (
            patch.object(core, "_get_files_from_stac", return_value=None),
            patch.object(core.ds, "dataset") as dataset,
            patch.object(core.fs, "S3FileSystem"),
        ):
            core._prepare_query("building", SMALL_BBOX, "2026-08-19.0", 10, 30, True)  # noqa: SLF001

        target = dataset.call_args.args[0]
        self.assertIsInstance(
            target, str, "a failed STAC lookup should not widen the read to the whole theme, but it does"
        )

    def test_a_narrowed_lookup_passes_only_the_intersecting_files(self) -> None:
        """The contrast, so the test above is measuring the difference and not a constant."""
        from overturemaps import core

        with (
            patch.object(core, "_get_files_from_stac", return_value=["bucket/one.parquet"]),
            patch.object(core.ds, "dataset") as dataset,
            patch.object(core.fs, "S3FileSystem"),
        ):
            core._prepare_query("building", SMALL_BBOX, "2026-08-19.0", 10, 30, True)  # noqa: SLF001

        self.assertEqual(dataset.call_args.args[0], ["bucket/one.parquet"])


class TheGatewayRefusesTests(SimpleTestCase):
    """No narrowing, no lookup."""

    def setUp(self) -> None:
        super().setUp()
        _reset_breaker()
        self.addCleanup(_reset_breaker)
        _patch_rate_limit_gate(self)

    def test_it_raises_when_the_stac_index_is_unavailable(self) -> None:
        """Scanning the planet is never what we want, so refusing is the better answer."""
        gateway = OvertureMapsGateway()
        with (
            patch(_STAC_LOOKUP, return_value=None),
            patch(_GEODATAFRAME) as geodataframe,
            pytest.raises(GatewayRateLimitedError),
        ):
            gateway.get_buildings(SMALL_BBOX)

        geodataframe.assert_not_called()

    def test_it_proceeds_when_the_index_answers(self) -> None:
        """The contrast, so the test above is not passing for the wrong reason."""
        gateway = OvertureMapsGateway()
        with patch(_STAC_LOOKUP, return_value=["bucket/one.parquet"]), patch(_GEODATAFRAME) as geodataframe:
            gateway.get_buildings(SMALL_BBOX)

        geodataframe.assert_called_once()
        self.assertTrue(geodataframe.call_args.kwargs.get("stac"))

    def test_an_empty_result_is_not_a_refusal(self) -> None:
        """ "No buildings here" is an answer; the library returns an empty frame for it."""
        gateway = OvertureMapsGateway()
        with patch(_STAC_LOOKUP, return_value=[]), patch(_GEODATAFRAME) as geodataframe:
            gateway.get_buildings(SMALL_BBOX)

        geodataframe.assert_called_once()


class TheLatestReleaseIsResolvedTests(SimpleTestCase):
    """With no pinned release, the index lookup must name a real one.

    The library resolves "latest" inside its own read, but not inside `_get_files_from_stac`, so the
    narrowing check asked for `stac.overturemaps.org/None/collections.parquet`, got a 404, and refused
    every lookup."""

    def setUp(self) -> None:
        super().setUp()
        _reset_breaker()
        self.addCleanup(_reset_breaker)
        _patch_rate_limit_gate(self)

    def test_the_index_lookup_and_the_read_name_the_latest_release(self) -> None:
        gateway = OvertureMapsGateway()
        with (
            patch(_LATEST_RELEASE, return_value="2026-09-17.0"),
            patch(_STAC_LOOKUP, return_value=["bucket/one.parquet"]) as lookup,
            patch(_GEODATAFRAME) as geodataframe,
        ):
            gateway.get_buildings(SMALL_BBOX)

        self.assertEqual(lookup.call_args.args[3], "2026-09-17.0")
        self.assertEqual(geodataframe.call_args.kwargs["release"], "2026-09-17.0")

    def test_the_real_lookup_asks_for_a_release_url(self) -> None:
        """Through the library's own URL construction, not a mock of it."""
        from overturemaps import core

        gateway = OvertureMapsGateway()
        with (
            patch(_LATEST_RELEASE, return_value="2026-09-17.0"),
            patch.object(core, "urlopen", side_effect=OSError("offline")) as urlopen,
            pytest.raises(GatewayRateLimitedError),
        ):
            gateway.get_buildings(SMALL_BBOX)

        self.assertEqual(urlopen.call_args.args[0], "https://stac.overturemaps.org/2026-09-17.0/collections.parquet")

    def test_a_pinned_release_skips_the_catalog(self) -> None:
        gateway = OvertureMapsGateway(release="2026-08-19.0")
        with (
            patch(_LATEST_RELEASE, side_effect=AssertionError("a pinned release asked the catalog")),
            patch(_STAC_LOOKUP, return_value=["bucket/one.parquet"]) as lookup,
            patch(_GEODATAFRAME),
        ):
            gateway.get_buildings(SMALL_BBOX)

        self.assertEqual(lookup.call_args.args[3], "2026-08-19.0")

    def test_the_catalog_is_not_asked_on_every_lookup(self) -> None:
        gateway = OvertureMapsGateway()
        with (
            patch(_LATEST_RELEASE, return_value="2026-09-17.0") as latest,
            patch(_STAC_LOOKUP, return_value=["bucket/one.parquet"]),
            patch(_GEODATAFRAME),
        ):
            for _ in range(3):
                gateway.get_buildings(SMALL_BBOX)

        self.assertEqual(latest.call_count, 1)

    def test_an_unreachable_catalog_is_a_refusal(self) -> None:
        gateway = OvertureMapsGateway()
        with (
            patch(_LATEST_RELEASE, side_effect=OSError("HTTP Error 503")),
            patch(_STAC_LOOKUP) as lookup,
            patch(_GEODATAFRAME) as geodataframe,
            pytest.raises(GatewayRateLimitedError),
        ):
            gateway.get_buildings(SMALL_BBOX)

        lookup.assert_not_called()
        geodataframe.assert_not_called()


class TheBreakerStopsTheLoopTests(SimpleTestCase):
    """A refusal has to stop the next lookup, or the storm continues.

    Every queued enrichment task probing an index that is refusing us is the
    loop that earned the rate limit in the first place.
    """

    def setUp(self) -> None:
        super().setUp()
        _reset_breaker()
        self.addCleanup(_reset_breaker)
        _patch_rate_limit_gate(self)

    def test_the_index_is_not_probed_again_during_the_cooldown(self) -> None:
        gateway = OvertureMapsGateway()
        with patch(_STAC_LOOKUP, return_value=None) as lookup:
            for _ in range(5):
                with pytest.raises(GatewayRateLimitedError):
                    gateway.get_buildings(SMALL_BBOX)

        self.assertEqual(lookup.call_count, 1, "each refusal probed again, which is the storm this is meant to stop")

    def test_it_recovers_once_the_cooldown_passes(self) -> None:
        """A breaker that never re-closes is an outage of our own making."""
        from urbanlens.dashboard.services.apis.locations.boundaries import overture_maps

        gateway = OvertureMapsGateway()
        with patch(_STAC_LOOKUP, return_value=None), pytest.raises(GatewayRateLimitedError):
            gateway.get_buildings(SMALL_BBOX)

        overture_maps._stac_unavailable_until = 0.0  # noqa: SLF001 - as though the window had elapsed
        with patch(_STAC_LOOKUP, return_value=["bucket/one.parquet"]), patch(_GEODATAFRAME) as geodataframe:
            gateway.get_buildings(SMALL_BBOX)

        geodataframe.assert_called_once()


def _reset_breaker() -> None:
    """Close the circuit, so one test's refusal does not silence the next."""
    from urbanlens.dashboard.services.apis.locations.boundaries import overture_maps

    overture_maps._stac_unavailable_until = 0.0  # noqa: SLF001
    overture_maps._latest_release_cache = None  # noqa: SLF001


def _patch_rate_limit_gate(test_case: SimpleTestCase) -> None:
    """Make the P110 call-budget gate a no-op, so these `SimpleTestCase`s never touch the DB.

    Both halves are stubbed: `_reserve_call_budget` (so no `ApiCallLog` row is reserved) and
    `_finalize_call` (so `_fetch` has nothing to update afterwards). The gate's own behaviour
    (an exhausted budget refusing the call) is covered by `test_overture_call_budget.py`; every
    test here is about the STAC circuit breaker instead.
    """
    latest = patch(_LATEST_RELEASE, return_value="2026-09-17.0")
    latest.start()
    test_case.addCleanup(latest.stop)
    gate = patch.object(OvertureMapsGateway, "_reserve_call_budget", return_value=1)
    gate.start()
    test_case.addCleanup(gate.stop)
    finalize = patch("urbanlens.dashboard.services.apis.locations.boundaries.overture_maps._finalize_call")
    finalize.start()
    test_case.addCleanup(finalize.stop)


def _bbox():
    """The library's own bbox type, built the way it builds one."""
    from overturemaps import core

    return core._coerce_bbox(SMALL_BBOX)  # noqa: SLF001


class TheLookupCannotHangTests(SimpleTestCase):
    """The library gives its own HTTP call no timeout at all.

    `_get_files_from_stac` does `with urlopen(stac_url) as response`, with no `timeout=`."""

    def setUp(self) -> None:
        super().setUp()
        _reset_breaker()
        self.addCleanup(_reset_breaker)
        _patch_rate_limit_gate(self)

    def test_the_library_still_has_no_timeout_of_its_own(self) -> None:
        """Pinned so an upstream fix is noticed rather than silently duplicated."""
        import inspect

        from overturemaps import core

        source = inspect.getsource(core._get_files_from_stac)  # noqa: SLF001
        self.assertIn(
            "urlopen(stac_url)",
            source,
            "the library's STAC call changed shape; re-check whether our deadline is still needed",
        )

    def test_a_hanging_lookup_is_refused_rather_than_waited_on(self) -> None:
        from urbanlens.dashboard.services.apis.locations.boundaries import overture_maps

        gateway = OvertureMapsGateway()
        with (
            patch.object(overture_maps, "_STAC_LOOKUP_TIMEOUT_SECONDS", 0.05),
            patch(_STAC_LOOKUP, side_effect=lambda *a, **k: time.sleep(5)),
            patch(_GEODATAFRAME) as geodataframe,
            pytest.raises(GatewayRateLimitedError),
        ):
            gateway.get_buildings(SMALL_BBOX)

        geodataframe.assert_not_called()

    def test_a_hanging_lookup_opens_the_breaker_too(self) -> None:
        """A slow index is as useless as a refusing one, so it stops the storm the same way."""
        from urbanlens.dashboard.services.apis.locations.boundaries import overture_maps

        gateway = OvertureMapsGateway()
        with (
            patch.object(overture_maps, "_STAC_LOOKUP_TIMEOUT_SECONDS", 0.05),
            patch(_STAC_LOOKUP, side_effect=lambda *a, **k: time.sleep(5)) as lookup,
        ):
            for _ in range(3):
                with pytest.raises(GatewayRateLimitedError):
                    gateway.get_buildings(SMALL_BBOX)

        self.assertEqual(lookup.call_count, 1)
