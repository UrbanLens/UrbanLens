"""What the Wikimedia Commons gateway asks Commons, and which files it keeps as photos of the place."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any
from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.apis.assets.base import MediaItem
from urbanlens.dashboard.services.apis.assets.wikimedia import WikimediaGateway
from urbanlens.dashboard.services.pins.external_data import MediaPanelSource, get_panel_source

_BOOK_SCAN = "File:American Ancestry 4.djvu"
_PHOTO = "File:Hudson River Psychiatric Center front view.jpg"

# Shapes as Commons returned them for the HRSH queries on 2026-10-02.
_IMAGEINFO = {
    _BOOK_SCAN: {
        "url": "https://upload.wikimedia.org/wikipedia/commons/2/28/American_Ancestry_4.djvu",
        "thumburl": "https://upload.wikimedia.org/wikipedia/commons/thumb/2/28/American_Ancestry_4.djvu/page1-400px-American_Ancestry_4.djvu.jpg",
        "descriptionurl": "https://commons.wikimedia.org/wiki/File:American_Ancestry_4.djvu",
        "mime": "image/vnd.djvu",
        "mediatype": "OFFICE",
        "extmetadata": {"ImageDescription": {"value": "Special collection -- Munsell imprints"}},
    },
    _PHOTO: {
        "url": "https://upload.wikimedia.org/wikipedia/commons/6/61/Hudson_River_Psychiatric_Center_front_view.jpg",
        "thumburl": "https://upload.wikimedia.org/wikipedia/commons/thumb/6/61/Hudson_River_Psychiatric_Center_front_view.jpg/400px-Hudson_River_Psychiatric_Center_front_view.jpg",
        "descriptionurl": "https://commons.wikimedia.org/wiki/File:Hudson_River_Psychiatric_Center_front_view.jpg",
        "mime": "image/jpeg",
        "mediatype": "BITMAP",
        "extmetadata": {
            "ImageDescription": {"value": "Abandoned buildings near the entrance of Hudson River Psychiatric Center"}
        },
    },
}


def _response(payload: dict) -> mock.Mock:
    response = mock.Mock()
    response.status_code = 200
    response.json.return_value = payload
    response.raise_for_status = mock.Mock()
    return response


class _FakeCommons:
    """Answers the gateway's two calls, a File-namespace search and an imageinfo lookup, and records both."""

    def __init__(self, titles: list[str]) -> None:
        self.titles = titles
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, params: dict[str, Any], timeout: int) -> mock.Mock:
        self.calls.append(params)
        if params.get("list") == "search":
            return _response({"query": {"search": [{"title": title} for title in self.titles]}})
        pages = {}
        for n, title in enumerate(params["titles"].split("|")):
            info = dict(_IMAGEINFO[title])
            if "mediatype" not in params.get("iiprop", "").split("|"):
                info.pop("mediatype")
            pages[str(n)] = {"title": title, "imageinfo": [info]}
        return _response({"query": {"pages": pages}})


class WikimediaKeepsPhotosTests(SimpleTestCase):
    """A full-text File search also matches the OCR text of scanned books, whose page-1 thumbnail is a library cover."""

    def _search(self, titles: list[str]) -> tuple[list[dict[str, Any]], _FakeCommons]:
        gateway = WikimediaGateway()
        fake = _FakeCommons(titles)
        with mock.patch.object(gateway.session, "get", side_effect=fake):
            return gateway.search_images("Hudson River State Hospital 83 Hudson View Dr Poughkeepsie NY"), fake

    def test_a_scanned_book_is_not_returned_as_a_photo(self) -> None:
        results, _fake = self._search([_BOOK_SCAN, _PHOTO])

        self.assertEqual([result["title"] for result in results], ["Hudson River Psychiatric Center front view.jpg"])

    def test_the_media_type_it_filters_on_is_requested(self) -> None:
        _results, fake = self._search([_PHOTO])

        [imageinfo] = [call for call in fake.calls if "titles" in call]
        self.assertIn("mediatype", imageinfo["iiprop"].split("|"))


class WikimediaCachedRowsTests(SimpleTestCase):
    """Rows cached before the gateway stopped returning book scans must not keep showing one."""

    def test_a_cached_book_scan_is_dropped_on_read(self) -> None:
        panel = get_panel_source("wikimedia")
        assert isinstance(panel, MediaPanelSource)
        scan = MediaItem(
            url=_IMAGEINFO[_BOOK_SCAN]["url"],
            thumb_url=_IMAGEINFO[_BOOK_SCAN]["thumburl"],
            caption="Special collection -- Munsell imprints",
            source="Wikimedia Commons",
        )
        photo = MediaItem(
            url=_IMAGEINFO[_PHOTO]["url"],
            thumb_url=_IMAGEINFO[_PHOTO]["thumburl"],
            caption="",
            source="Wikimedia Commons",
        )
        cached = {"items": [asdict(scan), asdict(photo)]}

        self.assertEqual([item.url for item in panel.media_items(cached)], [photo.url])


class WikimediaSearchTermsTests(SimpleTestCase):
    """Commons matches each loose word anywhere in a file's text, so the place's name is searched as a phrase."""

    def _pin(self) -> Pin:
        location = Location(
            latitude="41.73328",
            longitude="-73.92812",
            street_number="83",
            route="Hudson View Dr",
            locality="Poughkeepsie",
            administrative_area_level_1="NY",
            official_name="Hudson River State Hospital",
        )
        pin = Pin()
        pin._state.fields_cache["location"] = location
        return pin

    def test_the_name_is_searched_as_a_phrase(self) -> None:
        terms = MediaPanelSource.search_terms(self._pin(), WikimediaGateway())

        self.assertTrue(terms)
        for term in terms:
            self.assertIn('"Hudson River State Hospital"', term)

    def test_the_narrow_query_is_the_phrase_and_its_locality(self) -> None:
        terms = MediaPanelSource.search_terms(self._pin(), WikimediaGateway())

        self.assertEqual(terms[-1], '"Hudson River State Hospital" Poughkeepsie NY')
