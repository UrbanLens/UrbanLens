"""What the Wikimedia Commons gateway asks Commons, and what it keeps of each file for judging relevance (P196)."""

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
from urbanlens.dashboard.services.pins.search_names import search_names

_BOOK_SCAN = "File:American Ancestry 4.djvu"
_COUNTY_HISTORY = "File:History of Duchess county, New York (IA cu31924100747272).pdf"
_PHOTO = "File:Cheney bldg Hudson River State Hospital NY3.jpg"
_PANORAMIO = "File:Fairview, NY, USA - panoramio.jpg"
_RECORDING = "File:Hudson River State Hospital tour.ogg"
_THUMB = "https://thumb.wikimedia.org/wikipedia/commons/thumb"
_UPLOAD = "https://upload.wikimedia.org/wikipedia/commons"
_UTM = "?utm_source=commons.wikimedia.org&utm_campaign=imageinfo&utm_content=original"

# Shapes as Commons returned them on 2026-10-03 for prop=imageinfo|coordinates with an extmetadata filter.
_PAGES: dict[str, dict[str, Any]] = {
    _BOOK_SCAN: {
        "imageinfo": [
            {
                "url": f"{_UPLOAD}/2/28/American_Ancestry_4.djvu{_UTM}",
                "thumburl": f"{_THUMB}/2/28/American_Ancestry_4.djvu/page1-500px-American_Ancestry_4.djvu.jpg",
                "descriptionurl": "https://commons.wikimedia.org/wiki/File:American_Ancestry_4.djvu",
                "mime": "image/vnd.djvu",
                "extmetadata": {
                    "ObjectName": {
                        "value": '<div class="fn">\nAmerican ancestry: : giving the name and descent</div>',
                        "source": "commons-desc-page",
                    },
                    "Categories": {
                        "value": "1887 books|DjVu files in English|Scans from the Internet Archive",
                        "source": "commons-categories",
                        "hidden": "",
                    },
                    "ImageDescription": {
                        "value": '<div class="description">\nSpecial collection -- Munsell imprints</div>',
                        "source": "commons-desc-page",
                    },
                },
            }
        ],
    },
    _COUNTY_HISTORY: {
        "imageinfo": [
            {
                "url": f"{_UPLOAD}/4/4a/History_of_Duchess_county.pdf{_UTM}",
                "thumburl": f"{_THUMB}/4/4a/History_of_Duchess_county.pdf/page1-500px-History_of_Duchess_county.pdf.jpg",
                "descriptionurl": "https://commons.wikimedia.org/wiki/File:History_of_Duchess_county.pdf",
                "mime": "application/pdf",
                "extmetadata": {
                    "ObjectName": {
                        "value": '<div class="fn">\nHistory of Duchess county, New York : with illustrations</div>',
                        "source": "commons-desc-page",
                    },
                    "Categories": {
                        "value": "History of Dutchess County, New York|Books from New York (state)",
                        "source": "commons-categories",
                        "hidden": "",
                    },
                },
            }
        ],
    },
    _PHOTO: {
        "imageinfo": [
            {
                "url": f"{_UPLOAD}/6/68/Cheney_bldg_Hudson_River_State_Hospital_NY3.jpg{_UTM}",
                "thumburl": f"{_THUMB}/6/68/Cheney_bldg_Hudson_River_State_Hospital_NY3.jpg/500px-Cheney.jpg",
                "descriptionurl": "https://commons.wikimedia.org/wiki/File:Cheney_bldg_Hudson_River_State_Hospital_NY3.jpg",
                "mime": "image/jpeg",
                "extmetadata": {
                    "ObjectName": {
                        "value": "Cheney bldg Hudson River State Hospital NY3",
                        "source": "mediawiki-metadata",
                    },
                    "Categories": {
                        "value": "Hudson River State Hospital|GFDL|June 2019 in New York (state)",
                        "source": "commons-categories",
                        "hidden": "",
                    },
                    "GPSLatitude": {"value": "41.727422", "source": "commons-desc-page"},
                    "GPSLongitude": {"value": "-73.931487", "source": "commons-desc-page"},
                    "ImageDescription": {
                        "value": "Cheney Building, Hudson River State Hospital, Poughkeepsie, New York, USA",
                        "source": "commons-desc-page",
                    },
                },
            }
        ],
        "coordinates": [{"lat": 41.727421666667, "lon": -73.931486666667, "primary": "", "globe": "earth"}],
    },
    _PANORAMIO: {
        "imageinfo": [
            {
                "url": f"{_UPLOAD}/1/1f/Fairview%2C_NY%2C_USA_-_panoramio.jpg{_UTM}",
                "thumburl": f"{_THUMB}/1/1f/Fairview%2C_NY%2C_USA_-_panoramio.jpg/500px-Fairview.jpg",
                "descriptionurl": "https://commons.wikimedia.org/wiki/File:Fairview,_NY,_USA_-_panoramio.jpg",
                "mime": "image/jpeg",
                "extmetadata": {
                    "ObjectName": {"value": "Fairview, NY, USA - panoramio", "source": "mediawiki-metadata"},
                    "GPSLatitude": {"value": "41.729481", "source": "commons-desc-page"},
                    "GPSLongitude": {"value": "-73.930259", "source": "commons-desc-page"},
                    "ImageDescription": {"value": "Fairview, NY, USA", "source": "commons-desc-page"},
                },
            }
        ],
    },
    _RECORDING: {
        "imageinfo": [
            {
                "url": f"{_UPLOAD}/0/0a/Hudson_River_State_Hospital_tour.ogg",
                "thumburl": "",
                "descriptionurl": "https://commons.wikimedia.org/wiki/File:Hudson_River_State_Hospital_tour.ogg",
                "mime": "application/ogg",
                "extmetadata": {},
            }
        ],
    },
}


def _response(payload: dict) -> mock.Mock:
    response = mock.Mock()
    response.status_code = 200
    response.json.return_value = payload
    response.raise_for_status = mock.Mock()
    return response


class _FakeCommons:
    """Answers the gateway's two calls, a File-namespace search and a page lookup, and records both.

    A page carries ``coordinates`` only when the lookup asks for them, as Commons does.
    """

    def __init__(self, titles: list[str]) -> None:
        self.titles = titles
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, params: dict[str, Any], timeout: int) -> mock.Mock:
        self.calls.append(params)
        if params.get("list") == "search":
            return _response({"query": {"search": [{"title": title} for title in self.titles]}})
        props = params.get("prop", "").split("|")
        pages = {}
        for n, title in enumerate(params["titles"].split("|")):
            page = {"title": title, "imageinfo": _PAGES[title]["imageinfo"]}
            if "coordinates" in props and "coordinates" in _PAGES[title]:
                page["coordinates"] = _PAGES[title]["coordinates"]
            pages[str(n)] = page
        return _response({"query": {"pages": pages}})


class _GatewayTestCase(SimpleTestCase):
    def media(self, titles: list[str]) -> tuple[list[MediaItem], _FakeCommons]:
        gateway = WikimediaGateway()
        fake = _FakeCommons(titles)
        with mock.patch.object(gateway.session, "get", side_effect=fake):
            return list(gateway._generate_media('"Hudson River State Hospital" Poughkeepsie NY')), fake

    def item(self, title: str) -> MediaItem:
        [item] = self.media([title])[0]
        return item


class WikimediaKeepsDocumentsTests(_GatewayTestCase):
    """Jess, 2026-10-02: returning a book is fine; only an irrelevant one is wrong. Relevance is judged on read."""

    def test_books_and_pdfs_are_returned_with_their_type(self) -> None:
        items, _fake = self.media([_BOOK_SCAN, _COUNTY_HISTORY, _PHOTO])

        self.assertEqual(
            {item.page_url.rsplit(":", 1)[-1]: item.content_type for item in items},
            {
                "American_Ancestry_4.djvu": "image/vnd.djvu",
                "History_of_Duchess_county.pdf": "application/pdf",
                "Cheney_bldg_Hudson_River_State_Hospital_NY3.jpg": "image/jpeg",
            },
        )
        self.assertTrue(self.item(_COUNTY_HISTORY).is_document)
        self.assertTrue(self.item(_BOOK_SCAN).is_document)
        self.assertFalse(self.item(_PHOTO).is_document)

    def test_sound_and_video_are_not_media_for_the_gallery(self) -> None:
        items, _fake = self.media([_RECORDING, _PHOTO])

        self.assertEqual(len(items), 1)

    def test_a_cached_book_is_not_dropped_for_being_a_book(self) -> None:
        panel = get_panel_source("wikimedia")
        assert isinstance(panel, MediaPanelSource)
        book = self.item(_BOOK_SCAN)

        self.assertEqual(panel.media_items({"items": [asdict(book)]}), [book])


class WikimediaKeepsWhatRelevanceReadsTests(_GatewayTestCase):
    def test_coordinates_are_asked_for_with_every_file(self) -> None:
        _items, fake = self.media([_PHOTO])

        [lookup] = [call for call in fake.calls if "titles" in call]
        self.assertIn("coordinates", lookup["prop"].split("|"))
        # Commons answers coordinates for 10 pages by default, and a lookup asks about up to 50.
        self.assertEqual(lookup["colimit"], "max")

    def test_a_files_coordinates_are_kept(self) -> None:
        photo = self.item(_PHOTO)

        self.assertAlmostEqual(photo.latitude, 41.727421666667)
        self.assertAlmostEqual(photo.longitude, -73.931486666667)

    def test_gps_metadata_stands_in_when_commons_has_no_coordinates_for_the_page(self) -> None:
        photo = self.item(_PANORAMIO)

        self.assertAlmostEqual(photo.latitude, 41.729481)
        self.assertAlmostEqual(photo.longitude, -73.930259)

    def test_a_file_with_no_location_has_none(self) -> None:
        book = self.item(_BOOK_SCAN)

        self.assertIsNone(book.latitude)
        self.assertIsNone(book.longitude)

    def test_title_description_and_categories_are_kept_as_plain_text(self) -> None:
        photo = self.item(_PHOTO)
        history = self.item(_COUNTY_HISTORY)

        self.assertEqual(photo.title, "Cheney bldg Hudson River State Hospital NY3")
        self.assertEqual(photo.description, "Cheney Building, Hudson River State Hospital, Poughkeepsie, New York, USA")
        self.assertEqual(photo.keywords, "Hudson River State Hospital|GFDL|June 2019 in New York (state)")
        self.assertEqual(history.title, "History of Duchess county, New York : with illustrations")
        self.assertEqual(history.caption, history.title)

    def test_the_caption_is_the_description_as_before(self) -> None:
        self.assertEqual(
            self.item(_PHOTO).caption, "Cheney Building, Hudson River State Hospital, Poughkeepsie, New York, USA"
        )
        self.assertEqual(self.item(_BOOK_SCAN).caption, "Special collection -- Munsell imprints")


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
            official_name_source="google_places",
        )
        pin = Pin()
        pin._state.fields_cache["location"] = location
        return pin

    def test_the_name_is_searched_as_a_phrase(self) -> None:
        pin = self._pin()
        terms = MediaPanelSource.search_terms(pin, WikimediaGateway(), search_names(pin).base)

        self.assertTrue(terms)
        for term in terms:
            self.assertIn('"Hudson River State Hospital"', term)

    def test_the_narrow_query_is_the_phrase_and_its_locality(self) -> None:
        pin = self._pin()
        terms = MediaPanelSource.search_terms(pin, WikimediaGateway(), search_names(pin).base)

        self.assertEqual(terms[-1], '"Hudson River State Hospital" Poughkeepsie NY')


class CommonsTrackingParametersTests(_GatewayTestCase):
    """P215: Commons began adding ``utm_`` parameters to file URLs on 2026-09-23; a file keeps one URL regardless."""

    def test_the_items_urls_carry_no_tracking_parameters(self) -> None:
        item = self.item(_PHOTO)

        self.assertEqual(item.url, f"{_UPLOAD}/6/68/Cheney_bldg_Hudson_River_State_Hospital_NY3.jpg")
        self.assertNotIn("utm_", item.thumb_url)
        self.assertNotIn("utm_", item.page_url)
