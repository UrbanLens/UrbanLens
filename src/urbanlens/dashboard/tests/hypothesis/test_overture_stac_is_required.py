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


class TheBreakerStopsTheLoopTests(SimpleTestCase):
    """A refusal has to stop the next lookup, or the storm continues.

    Every queued enrichment task probing an index that is refusing us is the
    loop that earned the rate limit in the first place.
    """

    def setUp(self) -> None:
        super().setUp()
        _reset_breaker()
        self.addCleanup(_reset_breaker)

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
