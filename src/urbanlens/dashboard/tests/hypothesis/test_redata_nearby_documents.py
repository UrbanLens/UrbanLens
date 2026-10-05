"""Wikipedia and Wikidata material placed near a pin, from REData's ``reference-documents/``, on Article > Sources."""

from __future__ import annotations

import html
import re
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker
import pytest

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.plugins.builtin.redata_nearby_documents import NearbyReferenceDocumentsSource
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import (
    LocationContextEnvelope,
    LocationContextUnavailableError,
)
from urbanlens.dashboard.services.apis.locations.redata_reference_documents_gateway import (
    RedataReferenceDocumentsGateway,
)
from urbanlens.dashboard.services.locations import redata_point_data
from urbanlens.dashboard.services.pins.search_names import search_names
from urbanlens.UrbanLens.settings.app import settings

_SCHEDULE = "urbanlens.dashboard.services.pins.external_data.schedule_panel_fetch"
_SOURCE_URL = re.compile(r'data-source-url="([^"]*)"')


def _document(provider: str, title: str, url: str, **overrides: object) -> dict:
    """A row in REData's ``ReferenceDocumentSerializer`` shape."""
    row = {
        "uuid": "9b1f6a52-0c1d-4b6e-9a2f-6c0e8f7d1a23",
        "provider": provider,
        "external_id": f"{provider}:{title}",
        "kind": "article" if provider == "wikipedia" else "other",
        "title": title,
        "description": "",
        "url": url,
        "thumbnail_url": "",
        "date_text": "",
        "creator": "Wikipedia contributors" if provider == "wikipedia" else "",
        "license": "CC BY-SA 4.0" if provider == "wikipedia" else "CC0 1.0",
        "latitude": None,
        "longitude": None,
        "distance_meters": 120.0,
        "attributes": {},
        "record_retrieved_at": "2026-09-01T00:00:00Z",
    }
    row.update(overrides)
    return row


MILL_ARTICLE = _document(
    "wikipedia",
    "Smalltown Paper Mill",
    "https://en.wikipedia.org/wiki/Smalltown_Paper_Mill",
    attributes={"pageid": 4242, "language": "en"},
)
LIBRARY_ARTICLE = _document(
    "wikipedia",
    "Smalltown Free Library",
    "https://en.wikipedia.org/wiki/Smalltown_Free_Library",
    attributes={"pageid": 77, "language": "en"},
)
MILL_ENTITY = _document(
    "wikidata",
    "Smalltown Paper Mill",
    "https://www.wikidata.org/wiki/Q123",
    date_text="1890-01-01",
    creator="H. H. Richardson",
    attributes={
        "wikidata_id": "Q123",
        "instance_of": "paper mill",
        "heritage_designation": "NRHP",
        "architect": "H. H. Richardson",
    },
)


class ReferenceDocumentsNearGatewayTests(SimpleTestCase):
    def test_asks_the_near_point_path_and_returns_the_envelope(self) -> None:
        response = mock.Mock(status_code=200)
        response.json.return_value = {
            "count": 1,
            "complete": True,
            "results": [MILL_ENTITY],
            "providers": [{"provider": "wikidata", "status": "ok", "count": 1, "message": "", "radius_meters": 1000}],
        }
        session = mock.Mock()
        session.get.return_value = response
        gateway = RedataReferenceDocumentsGateway(base_url="https://redata.example.test", api_key="k", session=session)

        envelope = gateway.near(41.7, -73.9)

        self.assertTrue(session.get.call_args.args[0].endswith("/api/v1/reference-documents/"))
        self.assertEqual(session.get.call_args.kwargs["params"], {"lat": 41.7, "lng": -73.9})
        self.assertEqual(envelope.results[0]["attributes"]["heritage_designation"], "NRHP")


class NearbyReferenceDocumentsSourceTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.location = baker.make(Location, latitude="41.700000", longitude="-73.900000", google_place=None)
        self.pin = baker.make(Pin, profile=self.user.profile, location=self.location, name="Smalltown Paper Mill")
        self.source = NearbyReferenceDocumentsSource()

    def _fetch(self, envelope: LocationContextEnvelope) -> None:
        with (
            mock.patch.object(settings, "redata_api_url", "https://redata.example.test"),
            mock.patch.object(settings, "redata_api_key", "k"),
            mock.patch.object(redata_point_data, "reference_documents_near", return_value=envelope),
        ):
            self.source.fetch(self.pin)

    def test_gated_off_without_redata(self) -> None:
        with mock.patch.object(settings, "redata_api_url", None):
            self.assertFalse(self.source.gate(self.pin))

    def test_the_article_the_wikipedia_panel_matched_is_not_listed_twice(self) -> None:
        LocationCache.set(
            self.location, "wikipedia", {"title": "Smalltown Paper Mill", "url": MILL_ARTICLE["url"], "page_id": 4242}
        )
        self._fetch(LocationContextEnvelope(count=2, complete=True, results=[MILL_ARTICLE, MILL_ENTITY]))

        cached = LocationCache.get_fresh(self.location, "redata_reference_near")
        assert cached is not None
        self.assertEqual([row["provider"] for row in cached.data["documents"]], ["wikidata"])
        self.assertNotIn("record_retrieved_at", cached.data["documents"][0])

    def test_an_outage_is_not_cached_as_nothing_here(self) -> None:
        self._fetch(LocationContextEnvelope(count=0, complete=False, results=[]))
        self.assertIsNone(LocationCache.get_fresh(self.location, "redata_reference_near"))

    def test_a_failed_request_propagates_for_the_fetch_policy_to_suppress(self) -> None:
        with (
            mock.patch.object(settings, "redata_api_url", "https://redata.example.test"),
            mock.patch.object(settings, "redata_api_key", "k"),
            mock.patch.object(
                redata_point_data,
                "reference_documents_near",
                side_effect=LocationContextUnavailableError("source_error", "down"),
            ),
            pytest.raises(LocationContextUnavailableError),
        ):
            self.source.fetch(self.pin)


class NearbyReferenceDocumentsInSourcesTests(TestCase):
    """The listing as Article > Sources renders it, next to the sources Sources already fetched."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.location = baker.make(Location, latitude="41.700000", longitude="-73.900000", google_place=None)
        self.pin = baker.make(Pin, profile=self.user.profile, location=self.location, name="Smalltown Paper Mill")
        # The other sources Sources fetches itself have answered with nothing.
        LocationCache.set(self.location, "cris_building_usn", {}, query_key="q")
        for scope in search_names(self.pin).scopes:
            LocationCache.set(self.location, "wikimedia", {"items": []}, query_key="q", audience=scope.audience)
        self.enterContext(mock.patch(_SCHEDULE, return_value=True))

    def _listed(self) -> tuple[list[str], str]:
        response = self.client.get(reverse("pin.article.sources", kwargs={"pin_slug": self.pin.slug}))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        return [html.unescape(found) for found in _SOURCE_URL.findall(body)], html.unescape(body)

    def test_lists_what_names_the_place_and_says_what_wikidata_knows(self) -> None:
        LocationCache.set(
            self.location,
            "redata_reference_near",
            {"documents": [MILL_ARTICLE, LIBRARY_ARTICLE, MILL_ENTITY]},
            query_key="q",
        )
        urls, body = self._listed()
        self.assertEqual(urls, [MILL_ARTICLE["url"], MILL_ENTITY["url"]])
        self.assertIn("Wikidata · paper mill · NRHP · inception 1890 · architect H. H. Richardson · 120 m away", body)
