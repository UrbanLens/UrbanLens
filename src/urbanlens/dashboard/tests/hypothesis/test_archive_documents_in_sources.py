"""A PDF or DjVu an archive search found is listed under Article > Sources, from the gallery's own cached row (P260).

The Media gallery leaves documents out (P196), and only Commons and CRIS listed documents under Sources, so a document
among the Library of Congress, Internet Archive, Smithsonian, Digital Commonwealth or Chronicling America results was
shown nowhere. Sources reads the rows the gallery already cached for those archives and never searches them itself.
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
from urbanlens.dashboard.models.aliases.model import PinAlias, WikiAlias
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.images.relevance import media_item_key
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.place.model import Place, PlaceKind
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.apis.assets.base import MediaItem
from urbanlens.dashboard.services.pins.external_data import GalleryMediaSource, get_panel_source
from urbanlens.dashboard.services.pins.search_names import search_names
from urbanlens.dashboard.tests.hypothesis.redata_helpers import RedataConfiguredMixin

_SCHEDULE = "urbanlens.dashboard.services.pins.external_data.schedule_panel_fetch"
_SOURCE_URL = re.compile(r'data-source-url="([^"]*)"')
_PAGE_URL = re.compile(r'data-media-page-url="([^"]*)"')

#: Panel key -> the LocationCache source its gallery fills.
ARCHIVES = {
    "loc": "library_of_congress",
    "internet_archive": "internet_archive",
    "smithsonian": "smithsonian",
    "digital_commonwealth": "digital_commonwealth",
    "chronicling_america": "chronicling_america",
}


def _item(name: str, title: str, *, content_type: str = "application/pdf", **fields) -> MediaItem:
    return MediaItem(
        url=f"https://archive.test/files/{name}",
        thumb_url="",
        caption=title,
        source="Archive",
        page_url=f"https://archive.test/item/{name}",
        content_type=content_type,
        title=title,
        **fields,
    )


REPORT = _item("hrsh-report-1895.pdf", "Annual report of the Hudson River State Hospital, Poughkeepsie, N.Y., 1895")
SCAN = _item(
    "hrsh-plan.djvu", "Hudson River State Hospital, Poughkeepsie: plan of the grounds", content_type="image/vnd.djvu"
)
ELSEWHERE = _item("binghamton-report.pdf", "Annual report of the Binghamton State Hospital, Binghamton, N.Y.")
PHOTO = _item(
    "hrsh-cheney.jpg", "Cheney Building, Hudson River State Hospital, Poughkeepsie, New York", content_type="image/jpeg"
)
BY_OWN_NAME = _item("withers-register-1880.pdf", "The Withers Asylum, Poughkeepsie: register of patients, 1880")


class _ArchiveSourcesCase(RedataConfiguredMixin, TestCase):
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
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.pin = baker.make(Pin, profile=self.profile, location=self.location, name="Hudson River State Hospital")
        self.client.force_login(self.user)
        self.schedule = self.enterContext(mock.patch(_SCHEDULE, return_value=True))
        self.answer_every_fetched_source()

    def answer_every_fetched_source(self) -> None:
        """CRIS, Commons and the nearby Wikipedia/Wikidata listing, the sources Sources fetches itself, found nothing."""
        LocationCache.set(self.location, "cris_building_usn", {}, query_key="q")
        LocationCache.set(self.location, "redata_reference_near", {"documents": []}, query_key="q")
        for scope in search_names(self.pin).scopes:
            LocationCache.set(self.location, "wikimedia", {"items": []}, query_key="q", audience=scope.audience)

    def cache_archive(self, cache_source: str, items, audience: str = "") -> None:
        data = {"items": [asdict(item) for item in items], "search_names": {"names": [], "context": []}}
        LocationCache.set(self.location, cache_source, data, query_key="q", audience=audience)

    def listed(self, url: str) -> list[str]:
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return [html.unescape(found) for found in _SOURCE_URL.findall(response.content.decode())]

    def pin_sources(self, pin: Pin | None = None) -> list[str]:
        return self.listed(reverse("pin.article.sources", kwargs={"pin_slug": (pin or self.pin).slug}))

    def wiki_sources(self) -> list[str]:
        return self.listed(reverse("location.wiki.article.sources", kwargs={"location_slug": self.location.slug}))


class ArchiveDocumentListingTests(_ArchiveSourcesCase):
    def test_every_archive_lists_the_documents_its_gallery_cached(self) -> None:
        for key, cache_source in ARCHIVES.items():
            with self.subTest(archive=key):
                self.assertIsNotNone(get_panel_source(key), f"{key} is not registered")
                LocationCache.objects.filter(location=self.location, source__in=ARCHIVES.values()).delete()
                self.cache_archive(cache_source, [PHOTO, REPORT, SCAN])

                self.assertEqual(self.pin_sources(), [REPORT.page_url, SCAN.page_url])
                self.assertEqual(self.wiki_sources(), [REPORT.page_url, SCAN.page_url])

    def test_a_document_about_somewhere_else_is_not_listed(self) -> None:
        self.cache_archive("library_of_congress", [REPORT, ELSEWHERE])

        self.assertEqual(self.pin_sources(), [REPORT.page_url])
        self.assertEqual(self.wiki_sources(), [REPORT.page_url])

    def test_an_archive_document_opens_on_its_archives_page(self) -> None:
        self.cache_archive("internet_archive", [REPORT])
        response = self.client.get(
            reverse("pin.article.sources", kwargs={"pin_slug": self.pin.slug}),
            {"selected": f"internet_archive:{media_item_key(REPORT.url)}"},
        )

        self.assertContains(response, 'data-source-type="page"')
        self.assertNotContains(response, "<iframe")

    def test_the_document_proxy_fetches_nothing_for_an_archive(self) -> None:
        self.cache_archive("library_of_congress", [REPORT])
        url = reverse(
            "pin.article.sources.document",
            kwargs={"pin_slug": self.pin.slug, "source": "loc", "document_id": media_item_key(REPORT.url)},
        )
        with mock.patch("requests.Session.get") as fetch:
            response = self.client.get(url)

        self.assertEqual(response.status_code, 404)
        fetch.assert_not_called()

    def test_documents_are_still_not_gallery_tiles(self) -> None:
        self.cache_archive("library_of_congress", [PHOTO, REPORT, SCAN])
        response = self.client.get(reverse("pin.media", args=[self.pin.slug, "loc"]))
        tiles = {html.unescape(url) for url in _PAGE_URL.findall(response.content.decode())}

        self.assertEqual(tiles, {PHOTO.page_url})
        source = get_panel_source("loc")
        assert isinstance(source, GalleryMediaSource)
        data = source.cached_data(self.pin)
        assert data is not None
        from urbanlens.dashboard.services.media.subject_relevance import subject_for_pin

        self.assertEqual([item.url for item in source.gallery_items(data, subject_for_pin(self.pin))], [PHOTO.url])


class ArchiveDocumentFetchTests(_ArchiveSourcesCase):
    def test_sources_never_searches_an_archive(self) -> None:
        """No archive has a row here; Sources lists nothing from them, schedules none of them, and does not poll."""
        response = self.client.get(reverse("pin.article.sources", kwargs={"pin_slug": self.pin.slug}))

        scheduled = {call.args[0] for call in self.schedule.call_args_list}
        self.assertEqual(scheduled & set(ARCHIVES), set())
        self.assertNotContains(response, "attempt=")

    def test_the_wiki_never_searches_an_archive_either(self) -> None:
        response = self.client.get(
            reverse("location.wiki.article.sources", kwargs={"location_slug": self.location.slug})
        )

        scheduled = {call.args[0] for call in self.schedule.call_args_list}
        self.assertEqual(scheduled & set(ARCHIVES), set())
        self.assertNotContains(response, "attempt=")


class ArchiveDocumentConcealmentTests(_ArchiveSourcesCase):
    """A document found only by a pin's own names is shown only on that pin's page, as Commons' are (P188)."""

    def setUp(self) -> None:
        super().setUp()
        PinAlias.objects.create(pin=self.pin, name="Withers Asylum")
        self.answer_every_fetched_source()
        own = search_names(self.pin).own
        assert own is not None
        self.cache_archive("library_of_congress", [REPORT])
        self.cache_archive("library_of_congress", [BY_OWN_NAME], audience=own.audience)

    def test_the_pins_owner_sees_what_their_own_name_found(self) -> None:
        self.assertEqual(sorted(self.pin_sources()), sorted([REPORT.page_url, BY_OWN_NAME.page_url]))

    def test_the_wiki_shows_only_what_the_public_names_found(self) -> None:
        self.assertEqual(self.wiki_sources(), [REPORT.page_url])

    def test_another_accounts_pin_does_not_see_it(self) -> None:
        stranger = baker.make(User)
        their_pin = baker.make(
            Pin, profile=stranger.profile, location=self.location, name="Hudson River State Hospital"
        )
        self.client.force_login(stranger)

        self.assertEqual(self.pin_sources(their_pin), [REPORT.page_url])
