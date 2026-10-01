"""Regression guard for OvertureMapsGateway querying the whole planet per lookup."""

from __future__ import annotations

from unittest.mock import patch

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations.boundaries.overture_maps import OvertureMapsGateway


class OvertureMapsGatewayStacNarrowingTests(SimpleTestCase):
    def test_fetch_passes_stac_true_to_narrow_the_file_list(self) -> None:
        gateway = OvertureMapsGateway()
        with (
            # Mocking the lookup keeps this test about what it has always been about.
            patch(
                "urbanlens.dashboard.services.apis.locations.boundaries.overture_maps._intersecting_files",
                return_value=["bucket/one.parquet"],
            ),
            patch("overturemaps.core.get_latest_release", return_value="2026-09-17.0"),
            patch(
                "urbanlens.dashboard.services.apis.locations.boundaries.overture_maps._read_files"
            ) as mock_geodataframe,
            # The P110 call-budget gate (reserve/finalize) touches the DB (ApiCallLog/ApiRateLimit),
            # which SimpleTestCase forbids - the budget itself is covered separately in
            # test_overture_call_budget.py.
            patch.object(OvertureMapsGateway, "_reserve_call_budget", return_value=1),
            patch("urbanlens.dashboard.services.apis.locations.boundaries.overture_maps._finalize_call"),
        ):
            gateway.get_buildings((-71.059, 42.36, -71.058, 42.361))

        mock_geodataframe.assert_called_once()
        self.assertEqual(
            mock_geodataframe.call_args.args[0],
            ["bucket/one.parquet"],
            "the read must open only the narrowed files, or every lookup scans the entire global theme",
        )
