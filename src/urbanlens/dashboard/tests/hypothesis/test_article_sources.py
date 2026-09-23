"""Article > Sources: the document list and its scoped PDF proxy, on the private pin page and the wiki page."""

from __future__ import annotations

import re
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import caches
from django.test import Client
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin, PinType
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.plugins.builtin.cris_buildings import CrisBuildingPanelSource
from urbanlens.dashboard.services.apis.property_records.redata_gateway import (
    PropertyRecordsUnavailableError,
    RedataGateway,
)
from urbanlens.dashboard.services.geo.geo_boundary import GeoBoundary

_NY_ISH = GeoBoundary.from_bboxes([(40.0, 45.0, -80.0, -73.0)])
_SCHEDULE = "urbanlens.dashboard.services.pins.external_data.schedule_panel_fetch"
_PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"


def _form(attachment_id: int, resource_uuid: str, subject: str, kind: str = "building", **extra) -> dict:
    return {
        "id": attachment_id,
        "kind": "document",
        "name": "Building Inventory Form",
        "content_type": "application/pdf",
        "resource_uuid": resource_uuid,
        "subject": subject,
        "subject_kind": kind,
        **extra,
    }


def _campus_payload() -> dict:
    return {
        "USNName": "BLDG 51/MAIN/ADMIN",
        "resource_uuid": "b-main",
        "attachments_fetched": True,
        "site_scope": True,
        "attachments": [
            _form(11, "b-main", "BLDG 51/MAIN/ADMIN"),
            {"id": 12, "kind": "photo", "content_type": "image/jpeg", "resource_uuid": "b-main"},
            _form(50, "dist-1", "Hudson River State Hospital", kind="site"),
            _form(21, "b-chapel", "BLDG 28/CATHOLIC CHAPEL", site_building=True),
            _form(31, "b-mortuary", "BLDG 45/MORTUARY & LAB", site_building=True),
        ],
    }


def _items(content: str) -> list[dict[str, str]]:
    """Every ``.article-source-item`` element's data attributes, in document order."""
    items = []
    for tag in re.findall(r"<[^>]*class=\"[^\"]*\barticle-source-item\b[^\"]*\"[^>]*>", content):
        items.append(dict(re.findall(r"data-source-([a-z]+)=\"([^\"]*)\"", tag)))
    return items


class _SourcesTestBase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        caches[settings.PROXIED_BYTES_CACHE].clear()
        boundary = patch.object(CrisBuildingPanelSource, "geo_boundary", _NY_ISH)
        boundary.start()
        self.addCleanup(boundary.stop)

        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client = Client()
        self.client.force_login(self.user)
        self.location = baker.make(Location, latitude="41.733280", longitude="-73.928120", google_place=None)
        self.pin = baker.make(
            Pin,
            profile=self.profile,
            location=self.location,
            pin_type=PinType.PARCEL,
            pin_type_is_user_provided=True,
        )
        self.wiki = baker.make(Wiki, location=self.location, pin_type=PinType.PARCEL, pin_type_is_user_provided=True)

    def cache_payload(self, payload: dict, location: Location | None = None) -> None:
        LocationCache.set(location or self.location, "cris_building_usn", payload, query_key="q")

    def pin_panel(self, **params):
        return self.client.get(reverse("pin.article.sources", kwargs={"pin_slug": self.pin.slug}), params)

    def pin_document(self, document_id: str, source: str = "cris_building", pin: Pin | None = None):
        slug = (pin or self.pin).slug
        return self.client.get(
            reverse(
                "pin.article.sources.document", kwargs={"pin_slug": slug, "source": source, "document_id": document_id}
            )
        )

    def wiki_panel(self, **params):
        return self.client.get(
            reverse("location.wiki.article.sources", kwargs={"location_slug": self.location.slug}), params
        )

    def wiki_document(self, document_id: str, source: str = "cris_building"):
        return self.client.get(
            reverse(
                "location.wiki.article.sources.document",
                kwargs={"location_slug": self.location.slug, "source": source, "document_id": document_id},
            )
        )


class PinSourcesPanelTests(_SourcesTestBase):
    def test_each_pdf_is_listed_with_its_proxy_url_and_building(self) -> None:
        self.cache_payload(_campus_payload())
        response = self.pin_panel()
        self.assertEqual(response.status_code, 200)
        items = _items(response.content.decode())
        self.assertEqual(len(items), 4, "the photo attachment is not a source document")
        self.assertEqual({item["provider"] for item in items}, {"cris_building"})
        self.assertEqual({item["type"] for item in items}, {"pdf"})
        expected_url = reverse(
            "pin.article.sources.document",
            kwargs={"pin_slug": self.pin.slug, "source": "cris_building", "document_id": "b-chapel.21"},
        )
        self.assertIn(expected_url, [item["url"] for item in items])

    def test_campus_documents_name_at_least_three_distinct_buildings(self) -> None:
        self.cache_payload(_campus_payload())
        buildings = {item.get("building") for item in _items(self.pin_panel().content.decode())} - {None, ""}
        self.assertEqual(buildings, {"BLDG 51/MAIN/ADMIN", "BLDG 28/CATHOLIC CHAPEL", "BLDG 45/MORTUARY &amp; LAB"})

    def test_the_site_record_is_listed_without_claiming_to_be_a_building(self) -> None:
        self.cache_payload(_campus_payload())
        items = _items(self.pin_panel().content.decode())
        district = next(item for item in items if item["url"].endswith("/dist-1.50/"))
        self.assertNotIn("building", district)

    def test_every_item_has_a_same_row_open_link_to_the_same_url(self) -> None:
        self.cache_payload(_campus_payload())
        content = self.pin_panel().content.decode()
        for item in _items(content):
            self.assertRegex(
                content,
                rf"<a[^>]*class=\"[^\"]*article-source-open-link[^\"]*\"[^>]*href=\"{re.escape(item['url'])}\"[^>]*target=\"_blank\"",
            )

    def test_no_viewer_is_shown_until_a_document_is_selected(self) -> None:
        self.cache_payload(_campus_payload())
        self.assertNotContains(self.pin_panel(), 'id="article-source-viewer"')

    def test_selecting_a_document_points_the_viewer_at_it(self) -> None:
        self.cache_payload(_campus_payload())
        response = self.pin_panel(selected="cris_building:b-mortuary.31")
        url = reverse(
            "pin.article.sources.document",
            kwargs={"pin_slug": self.pin.slug, "source": "cris_building", "document_id": "b-mortuary.31"},
        )
        self.assertRegex(
            response.content.decode(), rf"<iframe[^>]*id=\"article-source-viewer\"[^>]*src=\"{re.escape(url)}\""
        )

    def test_selecting_an_unlisted_document_shows_no_viewer(self) -> None:
        self.cache_payload(_campus_payload())
        self.assertNotContains(self.pin_panel(selected="cris_building:nope.1"), 'id="article-source-viewer"')

    def test_another_accounts_pin_is_404(self) -> None:
        stranger_pin = baker.make(Pin, profile=baker.make(User).profile, location=self.location)
        response = self.client.get(reverse("pin.article.sources", kwargs={"pin_slug": stranger_pin.slug}))
        self.assertEqual(response.status_code, 404)

    def test_a_missing_cache_row_schedules_a_fetch_and_polls(self) -> None:
        with patch(_SCHEDULE, return_value=True) as schedule:
            response = self.pin_panel()
        schedule.assert_called_once_with("cris_building", self.pin)
        self.assertEqual(_items(response.content.decode()), [])
        self.assertContains(response, "attempt=1")

    def test_a_building_scope_record_on_a_campus_pin_is_refetched_before_listing(self) -> None:
        """Listing one building's forms while the campus fetch runs would read as the whole answer."""
        payload = _campus_payload()
        payload["site_scope"] = False
        self.cache_payload(payload)
        with patch(_SCHEDULE, return_value=True) as schedule:
            response = self.pin_panel()
        schedule.assert_called_once()
        self.assertEqual(_items(response.content.decode()), [])

    def test_when_no_fetch_can_run_it_lists_what_it_has(self) -> None:
        payload = _campus_payload()
        payload["site_scope"] = False
        self.cache_payload(payload)
        with patch(_SCHEDULE, return_value=False):
            response = self.pin_panel()
        self.assertEqual(
            len(_items(response.content.decode())), 2, "a building-scope record lists its own and the site's"
        )
        self.assertNotContains(response, "attempt=")

    def test_the_poll_stops_once_its_budget_is_spent(self) -> None:
        with patch(_SCHEDULE, return_value=True) as schedule:
            response = self.pin_panel(attempt="999")
        schedule.assert_not_called()
        self.assertNotContains(response, "attempt=")

    def test_nothing_found_renders_an_empty_state_without_polling(self) -> None:
        self.cache_payload({})
        with patch(_SCHEDULE, return_value=True) as schedule:
            response = self.pin_panel()
        schedule.assert_not_called()
        self.assertEqual(_items(response.content.decode()), [])
        self.assertNotContains(response, "attempt=")

    def test_a_building_pin_lists_only_its_own_building_and_site(self) -> None:
        self.pin.pin_type = PinType.BUILDING
        self.pin.save()
        self.cache_payload(_campus_payload())
        urls = [item["url"] for item in _items(self.pin_panel().content.decode())]
        self.assertEqual([url.rstrip("/").rsplit("/", 1)[-1] for url in urls], ["b-main.11", "dist-1.50"])


class PinSourceDocumentTests(_SourcesTestBase):
    """The PDF proxy serves only documents this pin's Sources list names."""

    def _download(self, content: bytes = _PDF, content_type: str = "application/pdf"):
        return (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "download_cultural_resource_attachment", return_value=(content, content_type)),
        )

    def test_a_listed_document_is_served_as_a_pdf(self) -> None:
        self.cache_payload(_campus_payload())
        init, download = self._download()
        with init, download as mock_download:
            response = self.pin_document("b-chapel.21")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(response.content, _PDF)
        mock_download.assert_called_once_with("b-chapel", 21)

    def test_it_may_be_framed_by_this_site_only(self) -> None:
        self.cache_payload(_campus_payload())
        init, download = self._download()
        with init, download:
            response = self.pin_document("b-chapel.21")
        self.assertEqual(response["X-Frame-Options"], "SAMEORIGIN")
        self.assertIn("frame-ancestors 'self'", response["Content-Security-Policy"])
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertTrue(response["Content-Disposition"].startswith("inline"))

    def test_an_attachment_the_list_does_not_name_is_404_and_never_fetched(self) -> None:
        """Exploit: using this route as an open proxy onto any REData attachment."""
        self.cache_payload(_campus_payload())
        init, download = self._download()
        with init, download as mock_download:
            unlisted = self.pin_document("some-other-resource.7")
            photo = self.pin_document("b-main.12")
        self.assertEqual(unlisted.status_code, 404)
        self.assertEqual(photo.status_code, 404)
        mock_download.assert_not_called()

    def test_a_document_cached_for_another_location_is_404(self) -> None:
        elsewhere = baker.make(Location, latitude="42.650000", longitude="-73.750000", google_place=None)
        self.cache_payload({"attachments_fetched": True, "attachments": [_form(7, "far-away", "Elsewhere")]}, elsewhere)
        self.cache_payload(_campus_payload())
        init, download = self._download()
        with init, download as mock_download:
            response = self.pin_document("far-away.7")
        self.assertEqual(response.status_code, 404)
        mock_download.assert_not_called()

    def test_another_accounts_pin_is_404_and_never_fetched(self) -> None:
        """Exploit: reaching a document through a pin the requester does not own."""
        stranger_location = baker.make(Location, latitude="41.740000", longitude="-73.930000", google_place=None)
        stranger_pin = baker.make(Pin, profile=baker.make(User).profile, location=stranger_location)
        self.cache_payload(
            {"attachments_fetched": True, "attachments": [_form(9, "private", "Theirs")]}, stranger_location
        )
        init, download = self._download()
        with init, download as mock_download:
            response = self.pin_document("private.9", pin=stranger_pin)
        self.assertEqual(response.status_code, 404)
        mock_download.assert_not_called()

    def test_bytes_that_are_not_a_pdf_are_never_served(self) -> None:
        """Exploit: an upstream answer of HTML would otherwise run as script on this site's origin."""
        self.cache_payload(_campus_payload())
        init, download = self._download(b"<html><script>alert(document.cookie)</script></html>", "text/html")
        with init, download:
            response = self.pin_document("b-chapel.21")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(b"<script>", response.content)

    def test_html_labelled_as_a_pdf_is_never_served(self) -> None:
        self.cache_payload(_campus_payload())
        init, download = self._download(b"<html><script>alert(1)</script></html>", "application/pdf")
        with init, download:
            response = self.pin_document("b-chapel.21")
        self.assertEqual(response.status_code, 404)

    def test_an_unavailable_attachment_is_404(self) -> None:
        self.cache_payload(_campus_payload())
        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(
                RedataGateway,
                "download_cultural_resource_attachment",
                side_effect=PropertyRecordsUnavailableError("attachment_unavailable", "gone"),
            ),
        ):
            response = self.pin_document("b-chapel.21")
        self.assertEqual(response.status_code, 404)

    def test_a_panel_that_lists_no_documents_is_404(self) -> None:
        self.cache_payload(_campus_payload())
        self.assertEqual(self.pin_document("b-chapel.21", source="smithsonian").status_code, 404)
        self.assertEqual(self.pin_document("b-chapel.21", source="no-such-source").status_code, 404)

    def test_a_second_view_reuses_the_cached_bytes(self) -> None:
        self.cache_payload(_campus_payload())
        init, download = self._download()
        with init, download as mock_download:
            self.pin_document("b-chapel.21")
            response = self.pin_document("b-chapel.21")
        self.assertEqual(response.content, _PDF)
        mock_download.assert_called_once()

    def test_an_anonymous_request_is_not_served(self) -> None:
        self.cache_payload(_campus_payload())
        self.client.logout()
        init, download = self._download()
        with init, download as mock_download:
            response = self.pin_document("b-chapel.21")
        self.assertNotEqual(response.status_code, 200)
        mock_download.assert_not_called()


class WikiSourcesTests(_SourcesTestBase):
    def test_the_wiki_lists_the_same_documents_through_its_own_proxy(self) -> None:
        self.cache_payload(_campus_payload())
        items = _items(self.wiki_panel().content.decode())
        self.assertEqual(len(items), 4)
        prefix = reverse("location.wiki.article", kwargs={"location_slug": self.location.slug})
        self.assertTrue(all(item["url"].startswith(prefix) for item in items), [item["url"] for item in items])
        buildings = {item.get("building") for item in items} - {None}
        self.assertGreaterEqual(len(buildings), 3)

    def test_a_wiki_document_is_served_as_a_pdf(self) -> None:
        self.cache_payload(_campus_payload())
        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(
                RedataGateway, "download_cultural_resource_attachment", return_value=(_PDF, "application/pdf")
            ),
        ):
            response = self.wiki_document("dist-1.50")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(response["X-Frame-Options"], "SAMEORIGIN")

    def test_an_unlisted_attachment_is_404_through_the_wiki_too(self) -> None:
        self.cache_payload(_campus_payload())
        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "download_cultural_resource_attachment") as mock_download,
        ):
            response = self.wiki_document("anything.1")
        self.assertEqual(response.status_code, 404)
        mock_download.assert_not_called()

    def test_a_wiki_the_viewer_cannot_see_is_404(self) -> None:
        """Exploit: reading a wiki's documents without access to the wiki."""
        self.cache_payload(_campus_payload())
        outsider = baker.make(User)
        self.client.force_login(outsider)
        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "download_cultural_resource_attachment") as mock_download,
        ):
            panel = self.wiki_panel()
            document = self.wiki_document("dist-1.50")
        self.assertEqual(panel.status_code, 404)
        self.assertEqual(document.status_code, 404)
        mock_download.assert_not_called()

    def test_a_cold_wiki_warms_through_the_viewers_own_pin(self) -> None:
        with patch(_SCHEDULE, return_value=True) as schedule:
            response = self.wiki_panel()
        schedule.assert_called_once_with("cris_building", self.pin)
        self.assertContains(response, "attempt=1")


class ArticleSubtabMarkupTests(_SourcesTestBase):
    """Both pages carry the Article sub-tab strip with a Sources tab wired to its endpoint."""

    def _assert_sources_tab(self, content: str, sources_url: str) -> None:
        self.assertRegex(content, r"class=\"[^\"]*article-subtabs[^\"]*\"[^>]*role=\"tablist\"")
        tab = re.search(r"<button[^>]*id=\"article-subtab-btn-sources\"[^>]*>", content)
        assert tab is not None, "no Sources tab button"
        self.assertIn('role="tab"', tab.group(0))
        self.assertIn("window.articleSetSubTab(this, 'sources')", tab.group(0))
        self.assertRegex(content, r"<div[^>]*data-article-subtab=\"sources\"[^>]*>")
        self.assertIn(f'hx-get="{sources_url}"', content)
        self.assertIn("window.articleSetSubTab = function", content)

    def test_the_wiki_page_has_the_sources_tab(self) -> None:
        response = self.client.get(reverse("location.wiki", kwargs={"location_slug": self.location.slug}))
        self.assertEqual(response.status_code, 200)
        self._assert_sources_tab(
            response.content.decode(),
            reverse("location.wiki.article.sources", kwargs={"location_slug": self.location.slug}),
        )

    def test_the_pin_page_has_the_sources_tab(self) -> None:
        response = self.client.get(reverse("pin.details", kwargs={"pin_slug": self.pin.slug}))
        self.assertEqual(response.status_code, 200)
        self._assert_sources_tab(
            response.content.decode(), reverse("pin.article.sources", kwargs={"pin_slug": self.pin.slug})
        )
