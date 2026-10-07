"""A newspaper page is judged by its own text, not by the dateline of the paper that printed it (P216).

The Library of Congress titles a Chronicling America page with its paper's dateline: "Image 2 of Western Kansas world
(WaKeeney, Kan.), August 8, 1908". That says where the paper was printed, not where a story on the page happened. In 1908
one syndicated article about the Hudson River State Hospital ran in Kansas, Minnesota, Michigan and Louisiana papers, and
the relevance rule dropped every Kansas, Michigan and Louisiana printing because "Kan.", "Mich." and "La." conflict with
New York.

The rows below are REData ``/reference-documents/search/`` results as REData 0.3.4 builds them from the live
collection. Titles, URLs and OCR excerpts were taken from the collection on 2026-10-06 and trimmed. Each goes through
the provider that parses it in production.
"""

from __future__ import annotations

from dataclasses import asdict, replace
from typing import Any
from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.assets.base import MediaItem
from urbanlens.dashboard.services.apis.locations import redata_reference_documents_gateway as archives
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope
from urbanlens.dashboard.services.media.subject_relevance import BoundingBox, MediaSubject
from urbanlens.dashboard.services.pins.external_data import get_panel_source

_LAT, _LNG = 41.7333, -73.9281
HRSH = MediaSubject(
    names=("Hudson River State Hospital", "HRSH", "HRPC"),
    latitude=_LAT,
    longitude=_LNG,
    bbox=BoundingBox.around(_LAT, _LNG, 600),
    city="Poughkeepsie",
    county="Dutchess County",
    state="NY",
    zipcode="12601",
    country="United States",
)
MANSION = replace(HRSH, names=("Historic Mansion",))

_SYNDICATED = (
    "LIVE FOR WEEKS IN THE BATHTUB Feature of New Treatment for Those Who Fear Insanity or Who Are Really Threatened "
    "with Mental Breakdown Novel Plans for Preventing the Dread Calamity of Madness Dr Charles W Pilgrim "
    "Superintendent of the Hudson River State Hospital for the Insane EW YORK states new Acute hospital on the grounds "
    "of the Hudson River State Hospital for the Insane at Poughkeep sie which will be opened next O"
)


def _page(
    title: str, url: str, description: str, *, city: str, county: str, state: str, newspaper: str
) -> dict[str, Any]:
    """One Chronicling America result as REData's ``ReferenceDocumentSerializer`` returns it."""
    return {
        "uuid": "00000000-0000-0000-0000-000000000000",
        "provider": "chronicling_america",
        "external_id": f"chronam:{url}",
        "kind": "article",
        "title": title,
        "description": description,
        "url": url,
        "thumbnail_url": "https://tile.loc.gov/image-services/iiif/service:ndnp:example/full/pct:6.25/0/default.jpg",
        "date_text": title.rsplit("), ", 1)[-1],
        "creator": newspaper,
        "license": "",
        "latitude": None,
        "longitude": None,
        "distance_meters": None,
        "attributes": {
            "newspaper": newspaper,
            "location_city": [city],
            "location_county": [county],
            "location_state": [state],
            "collection_coverage": "1794-1963 (public-domain historic newspapers)",
        },
        "record_retrieved_at": "2026-10-06T21:00:00Z",
    }


WAKEENEY = _page(
    "Image 2 of Western Kansas world (WaKeeney, Kan.), August 8, 1908",
    "https://www.loc.gov/resource/sn82015485/1908-08-08/ed-1/?sp=2",
    _SYNDICATED,
    city="wakeeney",
    county="trego",
    state="kansas",
    newspaper="western kansas world (wakeeney, kan.) 1885-current",
)
WELSH = _page(
    "Image 7 of The Rice belt journal (Welsh, Calcasieu Parish, La.), July 31, 1908",
    "https://www.loc.gov/resource/sn88064402/1908-07-31/ed-1/?sp=7",
    "FOR WEEKS IN THE BATHTUBj oture of New Treatment for Those Who Fear insanity or Who Are Really Threatened with "
    "Mental BreakdownNovel Plans for Preventing the Dread Calamity of Madness 0 pCharles W Pilgrim Superintendent of the "
    "Hudson River State Hospital for the Insane NW YORK states new Acute an hospital on the grounds of the an Hudson "
    "River State Hoslital f for the Insane at Poughkeep ar ele which will be opened ne",
    city="welsh",
    county="calcasieu",
    state="louisiana",
    newspaper="the rice belt journal (welsh, calcasieu parish, la.) 1900-19??",
)
LANSE = _page(
    "Image 2 of The L'Anse sentinel (L'Anse, L.S., Mich.), August 8, 1908",
    "https://www.loc.gov/resource/sn96077142/1908-08-08/ed-1/?sp=2",
    "LIVE FOR WEEKS IN THE BATHTUB Feature of New Treatment for Those Who Fear Insanity or Who Are Really Threatened "
    "with Mental Breakdown Novel Plans for Preventing the Dread Calamity of Madness Or Charles W Pilgrim Superintendent "
    "of the Hudson River State Hospital for the Insane Y v EW YORK states new Acute ll A I hospital on the grounds of the "
    "l Hudson River State Hospital JL l for the Insane at Poughkeep sle which wil",
    city="l'anse",
    county="baraga",
    state="michigan",
    newspaper="the l'anse sentinel (l'anse, l.s., mich.) 18??-current",
)
#: A front page the collection returned for the same query, whose text never names the hospital: LoC matches each word.
TRIBUNE = _page(
    "Image 1 of New-York tribune (New York [N.Y.]), August 1, 1897",
    "https://www.loc.gov/resource/sn83030214/1897-08-01/ed-1/?sp=1",
    "ribune or LVIL N 18522 NEWYORK SUNDAY AUGUST 1 18972 PARTS 22 PAGES WITH ILLUSTRATED SUPPLEMENT 20 PAGES PRICE "
    "FIVE CENTS THE NICARAGUAN TANGLE MINISTER MERRYS CASE CONSIDERED AT THE STATE DEPARTMENT A WAY OUT OF THE "
    "DIFFICULTY SUGGESTED BELIEF THAT THE UNION OF CENTRAL AMERICAN STATES WILL NOT BE a Washington July 31The State "
    "Department officials are trying to unravel the snarl",
    city="new york",
    county="new york",
    state="new york",
    newspaper="new-york tribune (new york [n.y.]) 1866-1924",
)


def _poughkeepsie_page(description: str) -> dict[str, Any]:
    """A page of the place's own town paper, so its dateline agrees with the place."""
    return _page(
        "Image 5 of Poughkeepsie eagle-news (Poughkeepsie, N.Y.), June 5, 1913",
        "https://www.loc.gov/resource/sn83031566/1913-06-05/ed-1/?sp=5",
        description,
        city="poughkeepsie",
        county="dutchess",
        state="new york",
        newspaper="poughkeepsie eagle-news (poughkeepsie, n.y.) 1907-1919",
    )


def _parsed(
    rows: list[dict[str, Any]],
    provider: type[archives._RedataReferenceDocumentProvider] = archives.ChroniclingAmericaMediaProvider,
) -> list[MediaItem]:
    """``rows`` as the gallery's provider turns REData's answer into media items."""
    with mock.patch.object(archives, "RedataReferenceDocumentsGateway") as gateway:
        gateway.return_value.search.return_value = LocationContextEnvelope(count=len(rows), complete=True, results=rows)
        return list(provider()._generate_media("Hudson River State Hospital Poughkeepsie NY"))


class _JudgedCase(SimpleTestCase):
    def assert_judged(self, subject: MediaSubject, rows: list[dict[str, Any]], *, relevant: bool) -> None:
        for item in _parsed(rows):
            with self.subTest(page=item.caption):
                judgement = subject.judge(item)
                self.assertEqual(judgement.relevant, relevant, judgement.reason)


class DatelineDoesNotVetoTests(_JudgedCase):
    def test_a_page_whose_story_names_the_place_is_kept_whatever_state_printed_it(self) -> None:
        self.assert_judged(HRSH, [WAKEENEY, WELSH, LANSE], relevant=True)

    def test_a_page_whose_text_never_names_the_place_is_still_dropped(self) -> None:
        self.assert_judged(HRSH, [TRIBUNE], relevant=False)

    def test_the_gallery_shows_the_syndicated_printings_and_not_the_front_page(self) -> None:
        source = get_panel_source("chronicling_america")
        data = {source.media_results_key: [asdict(item) for item in _parsed([WAKEENEY, TRIBUNE, WELSH, LANSE])]}

        shown = source.gallery_items(data, HRSH)

        self.assertEqual([item.url for item in shown], [row["url"] for row in (WAKEENEY, WELSH, LANSE)])


class DatelineDoesNotVouchTests(_JudgedCase):
    """The protections the rule exists for now rest on the page's text alone, since a dateline places no story."""

    def test_a_story_placing_the_name_in_another_city_is_dropped_even_in_the_places_own_paper(self) -> None:
        story = "BINGHAMTON, N. Y., June 4 Dr Charles W Pilgrim, late of the Hudson River State Hospital, opened the new wing here"
        self.assert_judged(HRSH, [_poughkeepsie_page(story)], relevant=False)

    def test_a_generic_name_is_not_placed_by_the_papers_town(self) -> None:
        self.assert_judged(
            MANSION, [_poughkeepsie_page("The historic mansion was sold at auction yesterday")], relevant=False
        )

    def test_a_generic_name_the_story_itself_places_is_kept(self) -> None:
        story = "The historic mansion at Poughkeepsie was sold at auction yesterday"
        self.assert_judged(MANSION, [_poughkeepsie_page(story)], relevant=True)


class OnlyTheLibraryOfCongressHasDatelinesTests(SimpleTestCase):
    def test_a_library_of_congress_search_hit_on_a_newspaper_page_is_judged_the_same(self) -> None:
        (item,) = _parsed([{**WELSH, "provider": "library_of_congress"}], archives.LibraryOfCongressMediaProvider)

        self.assertTrue(HRSH.matches(item), HRSH.judge(item).reason)

    def test_a_caption_shaped_like_a_dateline_elsewhere_is_still_read(self) -> None:
        caption = "Image 3 of Hudson River State Hospital (Poughkeepsie, N.Y.), May 1, 1925"
        commons = MediaItem(
            url="https://upload.wikimedia.org/wikipedia/commons/a/ab/Example.jpg",
            thumb_url="",
            caption=caption,
            source="Wikimedia Commons",
            page_url="https://commons.wikimedia.org/wiki/File:Example.jpg",
        )

        self.assertTrue(HRSH.matches(commons), HRSH.judge(commons).reason)

    def test_a_library_of_congress_photo_title_is_still_read(self) -> None:
        photo = MediaItem(
            url="https://tile.loc.gov/storage-services/service/pnp/example.jpg",
            thumb_url="",
            caption="Hudson River State Hospital, Poughkeepsie, N.Y.",
            source="Library of Congress",
            page_url="https://www.loc.gov/pictures/item/ny0000/",
        )

        self.assertTrue(HRSH.matches(photo), HRSH.judge(photo).reason)

    def test_a_malformed_url_is_judged_by_its_caption_and_does_not_raise(self) -> None:
        for url in ("https://[::1/x", "https://exa[mple.com/x", "http://["):
            with self.subTest(url=url):
                item = MediaItem(
                    url="", thumb_url="", caption="Hudson River State Hospital", source="Archive", page_url=url
                )
                self.assertTrue(HRSH.matches(item), HRSH.judge(item).reason)


class DistinctiveNameAloneTests(_JudgedCase):
    """The trade P216 makes, pinned so it is not undone by accident.

    A page naming the place by a distinctive name, with no geography of its own, is kept from any paper: that is how
    the Kansas and Michigan printings of the HRSH article pass, since their OCR mangles "Poughkeepsie" and "New York".
    The cost is that a distinctive name several towns share ("Mount Hope Cemetery") is kept from another town's paper
    too, as it already was from any other provider whose text has no geography.
    """

    def test_a_distinctive_name_alone_is_kept_whatever_paper_printed_it(self) -> None:
        cemetery = replace(
            HRSH, names=("Mount Hope Cemetery",), city="Rochester", county="Monroe County", zipcode="14620"
        )
        topeka = _page(
            "Image 3 of The Topeka state journal (Topeka, Kan.), June 1, 1908",
            "https://www.loc.gov/resource/sn82016014/1908-06-01/ed-1/?sp=3",
            "The funeral was held yesterday and burial was in Mount Hope Cemetery",
            city="topeka",
            county="shawnee",
            state="kansas",
            newspaper="the topeka state journal (topeka, kan.) 1892-1980",
        )
        self.assert_judged(cemetery, [topeka], relevant=True)
