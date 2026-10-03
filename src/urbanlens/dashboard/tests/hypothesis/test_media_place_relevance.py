"""Media galleries and Article > Sources show only what is about the place (P196), on every page that reads them.

One cached Wikimedia Commons row for Hudson River State Hospital holds what a real search returns: photos named for
the place, a photo geolocated on the campus with an unrelated caption, one geolocated in Binghamton, one across the
river, a scanned book and a county history that merely mention the words, and a report about the hospital.
"""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
import html
import re
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.gis.geos import MultiPolygon, Polygon
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.aliases.model import PinAlias, WikiAlias
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.images.relevance import MediaRelevance, media_item_key
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.place.model import Place, PlaceKind
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.apis.assets.base import MediaItem
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.services.photos.pin_photos import external_photos_for_pin
from urbanlens.dashboard.services.pins.external_data import get_panel_source
from urbanlens.dashboard.services.pins.search_names import search_names

_COMMONS = "https://commons.wikimedia.org/wiki/File:"
_UPLOAD = "https://upload.wikimedia.org/wikipedia/commons"


def _commons(name: str, caption: str, **fields) -> MediaItem:
    fields.setdefault("content_type", "image/jpeg")
    return MediaItem(
        url=f"{_UPLOAD}/a/ab/{name}",
        thumb_url=f"https://thumb.wikimedia.org/wikipedia/commons/thumb/a/ab/{name}/400px-{name}.jpg",
        caption=caption,
        source="Wikimedia Commons",
        page_url=f"{_COMMONS}{name}",
        **fields,
    )


ON_CAMPUS = _commons("Fairview,_NY,_USA_-_panoramio.jpg", "Fairview, NY, USA", latitude=41.729481, longitude=-73.930259)
NAMED = _commons(
    "Cheney_bldg_Hudson_River_State_Hospital_NY3.jpg",
    "Cheney Building, Hudson River State Hospital, Poughkeepsie, New York, USA",
    keywords="Hudson River State Hospital|GFDL",
)
IN_BINGHAMTON = _commons("Binghamton_asylum.jpg", "Hudson River State Hospital", latitude=42.1150, longitude=-75.8980)
ACROSS_THE_RIVER = _commons("IMG_0042.jpg", "IMG_0042", latitude=41.7333, longitude=-73.8950)
ANCESTRY = _commons(
    "American_Ancestry_4.djvu",
    "Special collection -- Munsell imprints",
    content_type="image/vnd.djvu",
    title="American ancestry: giving the name and descent",
)
COUNTY_HISTORY = _commons(
    "History_of_Duchess_county.pdf",
    "History of Duchess county, New York : with illustrations",
    content_type="application/pdf",
    title="History of Duchess county, New York : with illustrations",
    keywords="History of Dutchess County, New York|Books from New York (state)",
)
ANNUAL_REPORT = _commons(
    "Annual_report_of_the_Hudson_River_State_Hospital_1895.pdf",
    "The metadata below describe the original scanning.",
    content_type="application/pdf",
    title="Annual report of the managers of the Hudson River State Hospital at Poughkeepsie, 1895",
)
ITEMS = (ON_CAMPUS, NAMED, IN_BINGHAMTON, ACROSS_THE_RIVER, ANCESTRY, COUNTY_HISTORY, ANNUAL_REPORT)
GALLERY = {ON_CAMPUS.page_url, NAMED.page_url}
_PAGE_URL = re.compile(r'data-media-page-url="([^"]*)"')
_SOURCE_URL = re.compile(r'data-source-url="([^"]*)"')


def _page_urls(body: str) -> set[str]:
    return {html.unescape(url) for url in _PAGE_URL.findall(body)}


class _HrshTestCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is promoted to site admin
        campus = Polygon.from_bbox((-73.9380, 41.7240, -73.9200, 41.7400))
        campus.srid = 4326
        place = baker.make(Place, kind=PlaceKind.SITE, geometry=MultiPolygon(campus, srid=4326), area_sqm=2_000_000)
        self.location = baker.make(
            Location,
            latitude=Decimal("41.733300"),
            longitude=Decimal("-73.928100"),
            locality="Poughkeepsie",
            administrative_area_level_1="NY",
            administrative_area_level_2="Dutchess County",
            zipcode="12601",
            country="United States",
            official_name="Hudson River State Hospital",
            google_place=None,
            place=place,
        )
        self.wiki = baker.make(Wiki, location=self.location, name="Hudson River State Hospital")
        WikiAlias.objects.create(wiki=self.wiki, name="HRSH")
        WikiAlias.objects.create(wiki=self.wiki, name="HRPC")
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.pin = baker.make(Pin, profile=self.profile, location=self.location, name="Hudson River State Hospital")
        self.client.force_login(self.user)
        self.cache(ITEMS)
        for target in (
            "urbanlens.dashboard.services.pins.external_data.schedule_panel_fetch",
            "urbanlens.dashboard.services.photos.pin_photos.schedule_panel_fetch",
        ):
            patcher = mock.patch(target, return_value=False)
            patcher.start()
            self.addCleanup(patcher.stop)

    def cache(self, items, audience: str = "") -> None:
        data = {"items": [asdict(item) for item in items], "search_names": {"names": [], "context": []}}
        LocationCache.set(
            self.location,
            "wikimedia",
            data,
            query_key='"Hudson River State Hospital" Poughkeepsie NY',
            audience=audience,
        )

    def pin_gallery(self) -> set[str]:
        response = self.client.get(reverse("pin.media", args=[self.pin.slug, "wikimedia"]))
        self.assertEqual(response.status_code, 200)
        return _page_urls(response.content.decode())

    def sources(self, url: str) -> set[str]:
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return {html.unescape(found) for found in _SOURCE_URL.findall(response.content.decode())}


class GalleryTests(_HrshTestCase):
    def test_the_pin_gallery_shows_only_what_is_about_the_place(self) -> None:
        self.assertEqual(self.pin_gallery(), GALLERY)

    def test_the_wiki_gallery_shows_only_what_is_about_the_place(self) -> None:
        response = self.client.get(reverse("location.wiki.media", args=[self.location.slug, "wikimedia"]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(_page_urls(response.content.decode()), GALLERY)

    def test_the_photos_tab_lists_only_what_is_about_the_place(self) -> None:
        photos = external_photos_for_pin(self.pin, self.profile, self.user).photos

        self.assertEqual({photo.page_url for photo in photos}, GALLERY)

    def test_the_external_api_leaves_out_what_is_not_about_the_place(self) -> None:
        api_key, raw = generate_api_key(self.user, "Mobile")
        ApiKey.objects.filter(pk=api_key.pk).update(scopes=[ApiKeyScope.PANELS_READ.value])
        url = reverse("external_api:pins.panels.detail", kwargs={"pin_slug": self.pin.slug, "panel_key": "wikimedia"})

        response = self.client.get(url, HTTP_AUTHORIZATION=f"Bearer {raw}")

        self.assertEqual(response.status_code, 200)
        listed = {entry["page_url"] for entry in response.json()["media"]}
        self.assertEqual(listed, {*GALLERY, ANNUAL_REPORT.page_url})

    def test_an_item_the_viewer_marked_relevant_stays(self) -> None:
        MediaRelevance.objects.create(
            profile=self.profile,
            location=self.location,
            source="wikimedia",
            item_key=media_item_key(ACROSS_THE_RIVER.url),
            is_relevant=True,
        )

        self.assertEqual(self.pin_gallery(), {*GALLERY, ACROSS_THE_RIVER.page_url})

    def test_the_pins_own_name_finds_what_the_place_name_does_not(self) -> None:
        PinAlias.objects.create(pin=self.pin, name="Withers Asylum")
        own = search_names(self.pin).own
        assert own is not None
        by_alias = _commons("Withers_asylum_1880.jpg", "The Withers Asylum, Poughkeepsie, about 1880")
        self.cache(ITEMS)
        self.cache([by_alias], audience=own.audience)

        self.assertEqual(self.pin_gallery(), {*GALLERY, by_alias.page_url})
        wiki = self.client.get(reverse("location.wiki.media", args=[self.location.slug, "wikimedia"]))
        self.assertNotIn(by_alias.page_url, _page_urls(wiki.content.decode()))


class GoogleImagesTests(_HrshTestCase):
    """An address search is a text search like any other: HRSH's real results matched "83" and "138", not the place."""

    ARTWORK = {
        "title": "Woman in a Garden (1882\u201383) // Berthe Morisot (French, 1841\u20131895)",
        "link": "https://artic.edu/artworks/153798",
        "snippet": "Oil on canvas // 123 \u00d7 94 cm",
        "thumbnail": "https://www.artic.edu/iiif/2//5edb357d-2e8f-8673-d9e8-4b1150af3895/full/843,/0/default.jpg",
    }
    MOVIE = {
        "title": "The Menu (2022) - IMDb",
        "link": "https://www.imdb.com/title/tt9764362/",
        "snippet": "The Menu (2022) - IMDb",
        "thumbnail": "https://m.media-amazon.com/images/M/menu.jpg",
    }
    ABOUT_THE_PLACE = {
        "title": "Hudson River State Hospital, Poughkeepsie NY",
        "link": "https://example.org/hrsh",
        "snippet": "",
        "thumbnail": "https://example.org/hrsh.jpg",
    }

    def setUp(self) -> None:
        super().setUp()
        LocationCache.set(
            self.location,
            "google_images",
            {"items": [self.ARTWORK, self.MOVIE, self.ABOUT_THE_PLACE]},
            query_key="138 Hudson View Dr, Fairview, New York",
        )

    def test_the_wiki_gallery_shows_only_the_result_about_the_place(self) -> None:
        response = self.client.get(reverse("location.wiki.media", args=[self.location.slug, "google_images"]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(_page_urls(response.content.decode()), {self.ABOUT_THE_PLACE["link"]})

    def test_the_snippet_is_read_when_the_title_does_not_name_the_place(self) -> None:
        from urbanlens.dashboard.services.media.subject_relevance import subject_for_pin

        named_in_snippet = {
            **self.MOVIE,
            "link": "https://example.org/listing",
            "snippet": "Hudson River State Hospital, Poughkeepsie, NY",
        }
        panel = get_panel_source("google_images")

        items = panel.gallery_items({"items": [self.MOVIE, named_in_snippet]}, subject_for_pin(self.pin))

        self.assertEqual([item.page_url for item in items], [named_in_snippet["link"]])


class SourcesTests(_HrshTestCase):
    def test_a_report_about_the_place_is_a_source_on_the_pin(self) -> None:
        listed = self.sources(reverse("pin.article.sources", kwargs={"pin_slug": self.pin.slug}))

        self.assertEqual(listed, {ANNUAL_REPORT.page_url})

    def test_a_report_about_the_place_is_a_source_on_the_wiki(self) -> None:
        listed = self.sources(reverse("location.wiki.article.sources", kwargs={"location_slug": self.location.slug}))

        self.assertEqual(listed, {ANNUAL_REPORT.page_url})

    def test_a_document_opens_on_commons_rather_than_through_this_site(self) -> None:
        response = self.client.get(
            reverse("pin.article.sources", kwargs={"pin_slug": self.pin.slug}),
            {"selected": f"wikimedia:{media_item_key(ANNUAL_REPORT.url)}"},
        )

        self.assertContains(response, 'data-source-type="page"')
        self.assertNotContains(response, "<iframe")
        self.assertContains(response, f'href="{ANNUAL_REPORT.page_url}"')

    def test_documents_are_not_gallery_tiles(self) -> None:
        gallery = self.pin_gallery()

        for document in (ANCESTRY, COUNTY_HISTORY, ANNUAL_REPORT):
            with self.subTest(document=document.page_url):
                self.assertNotIn(document.page_url, gallery)

    def test_the_wikimedia_proxy_serves_no_bytes(self) -> None:
        document_id = media_item_key(ANNUAL_REPORT.url)
        url = reverse(
            "pin.article.sources.document",
            kwargs={"pin_slug": self.pin.slug, "source": "wikimedia", "document_id": document_id},
        )
        with mock.patch("requests.Session.get") as fetch:
            response = self.client.get(url)

        self.assertEqual(response.status_code, 404)
        fetch.assert_not_called()


class WikipediaArticleImagesTests(_HrshTestCase):
    """Article images are skipped when the Commons tab already shows them, and only then."""

    def test_an_image_the_commons_tab_leaves_out_is_not_skipped(self) -> None:
        from urbanlens.dashboard.services.apis.assets.wikipedia import WikipediaMediaGateway

        LocationCache.set(self.location, "wikipedia", {"title": "Hudson River State Hospital"}, query_key="q")
        with mock.patch.object(WikipediaMediaGateway, "get_media", autospec=True) as get_media:
            get_panel_source("wikipedia_media").fetch(self.pin)

        gateway = get_media.call_args.args[0]
        self.assertEqual(gateway.known_urls, frozenset({ON_CAMPUS.url, NAMED.url, ANNUAL_REPORT.url}))


class PanelSourceTests(_HrshTestCase):
    """The same judgement, at the panel source every page reads through."""

    def test_relevant_items_and_documents(self) -> None:
        from urbanlens.dashboard.services.media.subject_relevance import subject_for_pin

        panel = get_panel_source("wikimedia")
        entry = panel.cached_entry(self.pin)
        assert entry is not None

        relevant = panel.relevant_media_items(entry.data, subject_for_pin(self.pin))

        self.assertEqual({item.page_url for item in relevant}, {*GALLERY, ANNUAL_REPORT.page_url})
