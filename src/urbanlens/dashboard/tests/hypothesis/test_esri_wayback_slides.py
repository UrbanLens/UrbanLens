"""Esri Wayback slides must point at a tile Esri actually serves.

Wayback has no ``MapServer/export`` endpoint - it answers 404 with an HTML page, which Chrome's
opaque-response blocking reports as ``ERR_BLOCKED_BY_ORB`` on every pin page's imagery panel.
Each release is published only as a tile template (``itemURL``) addressed by level/row/col.
"""

from __future__ import annotations

import math
import re
from unittest import mock

from hypothesis import given, strategies as st
from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations.esri import EsriGateway

TEMPLATE = "https://wayback.maptiles.arcgis.com/arcgis/rest/services/World_Imagery/WMTS/1.0.0/default028mm/MapServer/tile/{release}/{{level}}/{{row}}/{{col}}"
TILE = re.compile(r"/tile/(?P<release>\d+)/(?P<level>\d+)/(?P<row>\d+)/(?P<col>\d+)$")


def _releases(*numbers: int) -> list[dict]:
    return [
        {"releaseNum": n, "releaseDateLabel": f"release {n}", "itemURL": TEMPLATE.format(release=n)} for n in numbers
    ]


def _tile_bounds(level: int, row: int, col: int) -> tuple[float, float, float, float]:
    """West, south, east, north of a Web Mercator tile."""
    n = 2**level

    def lat(y: int) -> float:
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))

    return col / n * 360 - 180, lat(row + 1), (col + 1) / n * 360 - 180, lat(row)


class WaybackSlideTests(SimpleTestCase):
    def _slides(self, latitude: float, longitude: float, releases: list[dict]) -> list:
        gateway = EsriGateway()
        with mock.patch.object(EsriGateway, "_get_wayback_releases", return_value=releases):
            return list(gateway.get_wayback_slides(latitude, longitude, max_count=len(releases)))

    def test_a_slide_is_a_release_tile_not_the_missing_export_endpoint(self) -> None:
        slides = self._slides(41.73328, -73.92812, _releases(64776))

        self.assertEqual(len(slides), 1)
        self.assertNotIn("MapServer/export", slides[0].img_src, "Wayback serves no export endpoint; this URL is a 404")
        self.assertIsNotNone(TILE.search(slides[0].img_src), f"not a level/row/col tile URL: {slides[0].img_src}")

    def test_each_slide_uses_its_own_release(self) -> None:
        slides = self._slides(41.73328, -73.92812, _releases(64776, 32246))

        self.assertEqual([TILE.search(s.img_src)["release"] for s in slides], ["64776", "32246"])

    def test_a_release_without_a_template_is_skipped(self) -> None:
        """Anti-vacuity: no guessed URL for a release Esri gave no address for."""
        slides = self._slides(41.73328, -73.92812, [{"releaseNum": 1, "releaseDateLabel": "x"}, *_releases(64776)])

        self.assertEqual(len(slides), 1)

    @given(latitude=st.floats(min_value=-80, max_value=80), longitude=st.floats(min_value=-179.9, max_value=179.9))
    def test_the_tile_contains_the_point(self, latitude: float, longitude: float) -> None:
        slide = self._slides(latitude, longitude, _releases(64776))[0]
        match = TILE.search(slide.img_src)
        assert match is not None
        west, south, east, north = _tile_bounds(int(match["level"]), int(match["row"]), int(match["col"]))

        # A point exactly on a tile edge may round into either neighbour.
        tolerance = 1e-9
        covered = (
            west - tolerance <= longitude <= east + tolerance and south - tolerance <= latitude <= north + tolerance
        )
        self.assertTrue(covered, f"{slide.img_src} does not cover ({latitude}, {longitude})")
