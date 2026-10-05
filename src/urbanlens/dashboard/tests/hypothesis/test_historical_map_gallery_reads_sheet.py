"""The Historical Maps gallery reads REData's ``/maps/`` match shape, which nests the catalogue record under ``sheet``."""

from __future__ import annotations

from urbanlens.core.tests.testcase import SimpleTestCase


def _match(**sheet: object) -> dict:
    """One ``MapMatchSerializer`` row, as REData's ``/maps/`` returns it for a point search."""
    return {
        "sheet": {
            "uuid": "5e0c3c4a-0000-0000-0000-000000000001",
            "title": "Poughkeepsie, Dutchess County, New York",
            "date_text": "1887",
            "attribution": "Library of Congress",
            "landing_page_url": "https://www.loc.gov/item/sanborn05977_001/",
            "thumbnail_url": "https://tile.loc.gov/image-services/iiif/service:gmd:sanborn/full/pct:12.5/0/default.jpg",
            "iiif_info_url": "https://tile.loc.gov/image-services/iiif/service:gmd:sanborn/info.json",
            **sheet,
        },
        "georeference": {"uuid": "5e0c3c4a-0000-0000-0000-000000000002", "bounds": [-73.93, 41.7, -73.92, 41.71]},
        "contains_point": True,
        "distance_meters": 0.0,
    }


class HistoricalMapGalleryTests(SimpleTestCase):
    def _items(self, rows: list[dict]):
        from urbanlens.dashboard.plugins.builtin.redata_historical_map_media import HistoricalMapMediaSource

        return HistoricalMapMediaSource().media_items({"maps": rows})

    def test_a_sheet_with_a_thumbnail_becomes_a_tile(self) -> None:
        items = self._items([_match()])

        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(
            item.thumb_url, "https://tile.loc.gov/image-services/iiif/service:gmd:sanborn/full/pct:12.5/0/default.jpg"
        )
        self.assertEqual(item.caption, "Poughkeepsie, Dutchess County, New York (1887)")
        self.assertEqual(item.source, "Library of Congress")
        self.assertEqual(item.page_url, "https://www.loc.gov/item/sanborn05977_001/")

    def test_the_full_view_is_a_bounded_rendering_of_the_scan(self) -> None:
        """A scanned sheet runs to tens of megapixels, so the lightbox asks the image service for a bounded size."""
        item = self._items([_match()])[0]

        self.assertEqual(
            item.url, "https://tile.loc.gov/image-services/iiif/service:gmd:sanborn/full/!1600,1600/0/default.jpg"
        )

    def test_a_sheet_without_an_image_service_opens_its_thumbnail(self) -> None:
        item = self._items([_match(iiif_info_url="")])[0]

        self.assertEqual(item.url, item.thumb_url)

    def test_a_sheet_with_only_an_image_service_gets_a_thumbnail_from_it(self) -> None:
        item = self._items([_match(thumbnail_url="")])[0]

        self.assertEqual(
            item.thumb_url, "https://tile.loc.gov/image-services/iiif/service:gmd:sanborn/full/!400,400/0/default.jpg"
        )

    def test_a_sheet_with_no_preview_image_is_skipped(self) -> None:
        self.assertEqual(self._items([_match(thumbnail_url="", iiif_info_url="")]), [])

    def test_an_untitled_sheet_is_still_shown(self) -> None:
        item = self._items([_match(title="", date_text="")])[0]

        self.assertEqual(item.caption, "Historical map")
