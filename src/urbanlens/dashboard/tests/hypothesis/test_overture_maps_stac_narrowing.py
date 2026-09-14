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
            patch("overturemaps.core._get_files_from_stac", return_value=["bucket/one.parquet"]),
            patch(
                "urbanlens.dashboard.services.apis.locations.boundaries.overture_maps._overture_geodataframe"
            ) as mock_geodataframe,
        ):
            gateway.get_buildings((-71.059, 42.36, -71.058, 42.361))

        mock_geodataframe.assert_called_once()
        self.assertTrue(
            mock_geodataframe.call_args.kwargs.get("stac"),
            "OvertureMapsGateway must pass stac=True or every lookup scans the entire global theme",
        )
