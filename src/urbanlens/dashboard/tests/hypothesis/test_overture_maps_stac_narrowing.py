"""Regression guard for OvertureMapsGateway querying the whole planet per lookup.

``overturemaps.geodataframe()`` defaults to ``stac=False``, which skips the
STAC-geoparquet index that narrows a bbox query down to the handful of S3
files that actually intersect it. Without ``stac=True``, every building/
address/place lookup - however small the bbox - opens a pyarrow dataset over
the *entire* global theme (hundreds of multi-gigabyte partition files) and
relies on filter pushdown alone to prune it while scanning.

Verified against the live 2026-08-19.0 release: a ~111m bbox resolves to 1
intersecting file via STAC vs. 512 total files in the unfiltered dataset.
Workers calling this repeatedly (``auto_nest_building_pins``,
``classify_detail_marker``, boundary generation) drove Celery worker RSS to
several gigabytes each and triggered the kernel OOM killer - see
docs/PROBLEMS.md.
"""

from __future__ import annotations

from unittest.mock import patch

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations.boundaries.overture_maps import OvertureMapsGateway


class OvertureMapsGatewayStacNarrowingTests(SimpleTestCase):
    def test_fetch_passes_stac_true_to_narrow_the_file_list(self) -> None:
        gateway = OvertureMapsGateway()
        with patch("urbanlens.dashboard.services.apis.locations.boundaries.overture_maps._overture_geodataframe") as mock_geodataframe:
            gateway.get_buildings((-71.059, 42.36, -71.058, 42.361))

        mock_geodataframe.assert_called_once()
        self.assertTrue(mock_geodataframe.call_args.kwargs.get("stac"), "OvertureMapsGateway must pass stac=True or every lookup scans the entire global theme")
