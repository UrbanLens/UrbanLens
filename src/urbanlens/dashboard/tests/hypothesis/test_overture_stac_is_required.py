"""P110: a lookup the STAC index cannot narrow is refused, never widened to the whole theme."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations.boundaries.overture_maps import OvertureMapsGateway
from urbanlens.dashboard.services.core.gateway import GatewayRateLimitedError, GatewayRequestError

#: A bbox the size every caller here actually uses - a single building.
SMALL_BBOX = (-71.059, 42.36, -71.058, 42.361)

_MODULE = "urbanlens.dashboard.services.apis.locations.boundaries.overture_maps"
_GEODATAFRAME = f"{_MODULE}._read_files"
_STAC_LOOKUP = f"{_MODULE}._intersecting_files"
_LATEST_RELEASE = "overturemaps.core.get_latest_release"


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

        self.assertEqual(geodataframe.call_args.args[0], ["bucket/one.parquet"])

    def test_an_empty_result_is_not_a_refusal(self) -> None:
        """ "No buildings here" is an answer, read as an empty frame without touching the bucket.

        The library handed geopandas a None reader for it and crashed."""
        gateway = OvertureMapsGateway()
        with patch(_STAC_LOOKUP, return_value=[]), patch("pyarrow.dataset.dataset") as dataset:
            frame = gateway.get_buildings(SMALL_BBOX)
            boundary = gateway.get_boundary(42.3605, -71.0585)

        self.assertEqual(len(frame), 0)
        self.assertIsNone(boundary)
        dataset.assert_not_called()


class TheLatestReleaseIsResolvedTests(SimpleTestCase):
    """With no pinned release, the index lookup must name a real one.

    The library resolves "latest" inside its own read but not inside its index lookup, so the
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
        geodataframe.assert_called_once()

    def test_the_real_lookup_asks_for_a_release_url(self) -> None:
        """Through the real index fetch, not a mock of it."""
        gateway = OvertureMapsGateway()
        with (
            patch(_LATEST_RELEASE, return_value="2026-09-17.0"),
            patch(f"{_MODULE}.urlopen", side_effect=OSError("offline")) as urlopen,
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


class ARefusalIsRecordedAsAFailureTests(SimpleTestCase):
    """The reserved ApiCallLog row starts as a success; a refusal before the read must still close it as a failure."""

    def setUp(self) -> None:
        super().setUp()
        _reset_breaker()
        self.addCleanup(_reset_breaker)

    def test_an_unavailable_index_finalizes_the_reservation_as_failed(self) -> None:
        with (
            patch.object(OvertureMapsGateway, "_reserve_call_budget", return_value=7),
            patch(f"{_MODULE}._finalize_call") as finalize,
            patch(_LATEST_RELEASE, return_value="2026-09-17.0"),
            patch(_STAC_LOOKUP, return_value=None),
            pytest.raises(GatewayRateLimitedError),
        ):
            OvertureMapsGateway().get_buildings(SMALL_BBOX)

        finalize.assert_called_once()
        self.assertEqual(finalize.call_args.args[0], 7)
        self.assertFalse(finalize.call_args.kwargs["success"])


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
    overture_maps._stac_index_cache.clear()  # noqa: SLF001


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
    """The index lookup is reached from the request path, so it gets a deadline."""

    def setUp(self) -> None:
        super().setUp()
        _reset_breaker()
        self.addCleanup(_reset_breaker)
        _patch_rate_limit_gate(self)

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


def _index_bytes(rows: list[tuple[str, tuple[float, float, float, float]]]) -> bytes:
    """A STAC collections index shaped like 2026-08-19.0's: ``collection`` null on every row."""
    import io

    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pa.table(
        {
            "collection": pa.array([None] * len(rows), type=pa.string()),
            "assets": [{"aws": {"alternate": {"s3": {"href": f"s3://{key}"}}}} for key, _ in rows],
            "bbox": [dict(zip(("xmin", "ymin", "xmax", "ymax"), extent, strict=True)) for _, extent in rows],
        },
    )
    buffer = io.BytesIO()
    pq.write_table(table, buffer)
    return buffer.getvalue()


class TheIndexIsReadByPartitionTests(SimpleTestCase):
    """The library filters the index on ``collection``, which the live index leaves null everywhere."""

    RELEASE = "2026-08-19.0"
    HERE = (-71.0595, 42.3595, -71.0575, 42.3615)
    ELSEWHERE = (10.0, 10.0, 11.0, 11.0)

    def setUp(self) -> None:
        super().setUp()
        _reset_breaker()
        self.addCleanup(_reset_breaker)
        root = f"overturemaps-us-west-2/release/{self.RELEASE}"
        self.building_here = f"{root}/theme=buildings/type=building/part-1.parquet"
        rows = [
            (self.building_here, self.HERE),
            (f"{root}/theme=buildings/type=building/part-2.parquet", self.ELSEWHERE),
            (f"{root}/theme=buildings/type=building_part/part-3.parquet", self.HERE),
            (f"{root}/theme=places/type=place/part-4.parquet", self.HERE),
        ]
        from unittest.mock import MagicMock

        response = MagicMock()
        response.__enter__.return_value.read.return_value = _index_bytes(rows)
        urlopen = patch(f"{_MODULE}.urlopen", return_value=response)
        self.urlopen = urlopen.start()
        self.addCleanup(urlopen.stop)

    def _lookup(self, overture_type: str = "building", theme: str = "buildings") -> list[str] | None:
        from urbanlens.dashboard.services.apis.locations.boundaries.overture_maps import _intersecting_files

        return _intersecting_files(theme, overture_type, SMALL_BBOX, self.RELEASE)

    def test_only_the_intersecting_files_of_the_type_are_returned(self) -> None:
        self.assertEqual(self._lookup(), [self.building_here])

    def test_a_sibling_type_sharing_a_prefix_is_not_matched(self) -> None:
        self.assertEqual(
            self._lookup("building_part"),
            [self.building_here.replace("type=building/part-1", "type=building_part/part-3")],
        )

    def test_the_index_is_fetched_once_per_release(self) -> None:
        self._lookup()
        self._lookup("place", "places")
        self.assertEqual(self.urlopen.call_count, 1)

    def test_an_unparseable_index_reads_as_unavailable(self) -> None:
        from urbanlens.dashboard.services.apis.locations.boundaries import overture_maps

        overture_maps._stac_index_cache.clear()  # noqa: SLF001
        self.urlopen.return_value.__enter__.return_value.read.return_value = b"<html>not parquet</html>"
        self.assertIsNone(self._lookup())


class TheNarrowedFilesAreWhatIsReadTests(SimpleTestCase):
    def test_the_dataset_is_opened_over_exactly_the_narrowed_files(self) -> None:
        from urbanlens.dashboard.services.apis.locations.boundaries.overture_maps import _read_files

        with (
            patch("pyarrow.dataset.dataset") as dataset,
            patch("pyarrow.fs.S3FileSystem"),
            patch("overturemaps.core._record_batch_reader_from_dataset", return_value=None),
            pytest.raises(GatewayRequestError),
        ):
            _read_files(["bucket/one.parquet"], SMALL_BBOX, connect_timeout=10, request_timeout=30)

        self.assertEqual(dataset.call_args.args[0], ["bucket/one.parquet"])
