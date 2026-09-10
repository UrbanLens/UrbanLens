"""P110: `stac=True` is a request, and the library grants it only when it can.

`test_overture_maps_stac_narrowing.py` guards that we ask for the narrowing.
This guards the half that turned out to matter more: what happens when Overture
refuses to answer.

`overturemaps/core.py`'s `_get_files_from_stac` catches every exception, prints,
and returns `None`; the caller then opens the entire theme rather than the
handful of intersecting partitions. So the OOM mitigation resolved on 2026-08-31
is switched off by Overture rate-limiting us — and enrichment calling Overture
per location is what earns the rate limit. The more work is queued, the more
certain the mitigation is to be off exactly when it is needed.

Observed 2026-09-10: a worker child at 1,743 MB inside `arrow_to_geopandas`,
reached from `enrich_wiki_location`, while the STAC index was answering
`HTTP Error 429`.

The reproductions are `xfail(strict=True)`: green while the gap stands, failing
the day the gateway learns to refuse instead of falling back.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations.boundaries.overture_maps import OvertureMapsGateway

#: A bbox the size every caller here actually uses - a single building.
SMALL_BBOX = (-71.059, 42.36, -71.058, 42.361)

_GEODATAFRAME = "urbanlens.dashboard.services.apis.locations.boundaries.overture_maps._overture_geodataframe"


class TheLibraryFallsBackToThePlanetTests(SimpleTestCase):
    """Pinning the third-party behaviour this depends on, so an upgrade is visible.

    Not a test of our code. It is here because the whole mitigation rests on a
    detail of someone else's error handling, and a release that changed it -
    in either direction - should be noticed here rather than in a worker's RSS.
    """

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


class TheGatewayShouldRefuseTests(SimpleTestCase):
    """What we want instead: no narrowing, no lookup."""

    @pytest.mark.xfail(strict=True, reason="P110: the gateway has no way to require the narrowing it asks for")
    def test_it_raises_when_the_stac_index_is_unavailable(self) -> None:
        """Falling back to scanning the planet is never what we want here."""
        from urbanlens.dashboard.services.core.gateway import GatewayRateLimitedError

        gateway = OvertureMapsGateway()
        with patch(_GEODATAFRAME, side_effect=_unavailable_stac), pytest.raises(GatewayRateLimitedError):
            gateway.get_buildings(SMALL_BBOX)

    @pytest.mark.xfail(strict=True, reason="P110: nothing checks the STAC index before the read")
    def test_it_checks_the_index_before_reading(self) -> None:
        """A probe through our own session would be rate-limited and guarded.

        The gateway sets `service_key = None`, so nothing it does today is
        visible to the rate limiter or to the outbound-call guard - the reads
        happen inside pyarrow's S3 filesystem, not through `self.session`.
        """
        gateway = OvertureMapsGateway()
        with patch(_GEODATAFRAME) as geodataframe, patch.object(type(gateway), "session", create=True) as session:
            gateway.get_buildings(SMALL_BBOX)

        self.assertTrue(session.get.called or session.head.called, "nothing probed the STAC index before the read")
        geodataframe.assert_called_once()


def _bbox():
    """The library's own bbox type, built the way it builds one."""
    from overturemaps import core

    return core._coerce_bbox(SMALL_BBOX)  # noqa: SLF001


def _unavailable_stac(*args: object, **kwargs: object) -> None:
    """Stand in for the library's silent fallback: it returns data, just far too much."""
    raise AssertionError("the gateway should have refused before calling the library")
