"""Tests for the REData-backed reference-document media providers (Smithsonian,
Library of Congress, Internet Archive) -
``services.apis.locations.redata_reference_documents_gateway``.

Mirrors ``test_redata_context_gateway.py``'s conventions: a mocked ``session``
for the gateway-level tests (no DB, no network), and a mocked
``RedataReferenceDocumentsGateway.search`` for the provider-level tests, since
those only care about turning a result dict into a ``MediaItem``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import mock

import pytest

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError
from urbanlens.dashboard.services.apis.locations.redata_reference_documents_gateway import (
    InternetArchiveMediaProvider,
    LibraryOfCongressMediaProvider,
    RedataReferenceDocumentsGateway,
    SmithsonianMediaProvider,
)

if TYPE_CHECKING:
    from urbanlens.dashboard.services.apis.assets.base import MediaItem, MediaProvider

    # See test_redata_media_gateway.py's identical trick: gives the mixin's own
    # methods real assertX()/provider_cls typing under mypy without unittest
    # actually discovering and running _ProviderMediaMappingMixin on its own
    # (it has no provider_cls/redata_provider/display_name set) - a
    # unittest.TestCase subclass is collected by class regardless of name, so
    # the real base must stay `object` at runtime.
    _MixinBase = SimpleTestCase
else:
    _MixinBase = object


def _response(status_code: int, body: object) -> mock.Mock:
    resp = mock.Mock(status_code=status_code)
    resp.json.return_value = body
    resp.text = ""
    return resp


def _gateway(session: mock.Mock) -> RedataReferenceDocumentsGateway:
    return RedataReferenceDocumentsGateway(base_url="https://redata.example.test", api_key="test-key", session=session)


class RedataReferenceDocumentsGatewaySearchTests(SimpleTestCase):
    def test_sends_q_and_provider_and_returns_results(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(
            200,
            {
                "count": 1,
                "complete": True,
                "results": [{"provider": "smithsonian", "title": "A Photo", "url": "https://example.test/a"}],
                "providers": [],
            },
        )

        results = _gateway(session).search("Bannerman Castle", provider="smithsonian", limit=10)

        self.assertEqual(results, [{"provider": "smithsonian", "title": "A Photo", "url": "https://example.test/a"}])
        params = session.get.call_args.kwargs["params"]
        self.assertEqual(params["q"], "Bannerman Castle")
        self.assertEqual(params["provider"], "smithsonian")
        self.assertEqual(params["limit"], 10)
        self.assertNotIn("lat", params)
        self.assertNotIn("lng", params)
        url = session.get.call_args.args[0]
        self.assertEqual(url, "https://redata.example.test/api/v1/reference-documents/search/")

    def test_lat_lng_are_region_hints_sent_together(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, {"count": 0, "complete": True, "results": [], "providers": []})

        _gateway(session).search("Bannerman Castle", latitude=41.5, longitude=-74.0)

        params = session.get.call_args.kwargs["params"]
        self.assertEqual(params["lat"], 41.5)
        self.assertEqual(params["lng"], -74.0)

    def test_unavailable_propagates(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(503, {"error": "all_providers_unavailable", "message": "down"})

        with pytest.raises(LocationContextUnavailableError):
            _gateway(session).search("Bannerman Castle", provider="smithsonian")


class _ProviderMediaMappingMixin(_MixinBase):
    """Shared assertions for each thin archive provider, run against a mocked
    ``RedataReferenceDocumentsGateway.search`` so these stay pure unit tests."""

    provider_cls: type[MediaProvider]
    redata_provider: str
    display_name: str

    def _items(self, results: list[dict], search_term: str = "Bannerman Castle") -> tuple[list[MediaItem], mock.Mock]:
        # See test_redata_media_gateway._slides: __post_init__ is neutralised
        # alongside search because the provider builds its own gateway from
        # module-level settings, whose constructor raises unless the machine
        # running the tests happens to have REData credentials configured.
        with (
            mock.patch.object(RedataReferenceDocumentsGateway, "__post_init__", return_value=None),
            mock.patch.object(RedataReferenceDocumentsGateway, "search", return_value=results) as mock_search,
        ):
            items = list(self.provider_cls()._generate_media(search_term))
        return items, mock_search

    def test_requests_its_own_provider_tag_with_a_clean_query(self) -> None:
        _items, mock_search = self._items([], "Bannerman Castle")
        mock_search.assert_called_once_with("Bannerman Castle", provider=self.redata_provider)

    def test_empty_search_term_never_calls_redata(self) -> None:
        with mock.patch.object(RedataReferenceDocumentsGateway, "search") as mock_search:
            items = list(self.provider_cls()._generate_media(""))
        self.assertEqual(items, [])
        mock_search.assert_not_called()

    def test_maps_title_url_and_thumbnail(self) -> None:
        results = [{"title": "The ruins", "url": "https://example.test/a", "thumbnail_url": "https://example.test/a-thumb", "date_text": "c. 1890", "license": "Public Domain"}]
        items, _ = self._items(results)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.url, "https://example.test/a")
        self.assertEqual(item.thumb_url, "https://example.test/a-thumb")
        self.assertEqual(item.caption, "The ruins")
        self.assertEqual(item.source, self.display_name)
        self.assertEqual(item.page_url, "https://example.test/a")

    def test_missing_thumbnail_falls_back_to_empty_string(self) -> None:
        items, _ = self._items([{"title": "No thumb", "url": "https://example.test/a"}])
        self.assertEqual(items[0].thumb_url, "")

    def test_skips_items_with_no_url(self) -> None:
        items, _ = self._items([{"title": "No URL"}])
        self.assertEqual(items, [])

    def test_service_key_is_the_historical_per_provider_tag(self) -> None:
        self.assertEqual(self.provider_cls.service_key, self.redata_provider)

    def test_quoting_is_left_to_redata_not_double_applied(self) -> None:
        """REData applies each provider's own quote_phrases server-side - this
        provider must not re-quote on top of it."""
        self.assertFalse(self.provider_cls.quote_name)
        self.assertFalse(self.provider_cls.quote_locality)

    def test_a_gateway_failure_propagates_rather_than_being_swallowed(self) -> None:
        with (
            mock.patch.object(RedataReferenceDocumentsGateway, "__post_init__", return_value=None),
            mock.patch.object(RedataReferenceDocumentsGateway, "search", side_effect=LocationContextUnavailableError("source_error", "down")),
            pytest.raises(LocationContextUnavailableError),
        ):
            list(self.provider_cls()._generate_media("Bannerman Castle"))


class SmithsonianMediaProviderTests(_ProviderMediaMappingMixin, SimpleTestCase):
    provider_cls = SmithsonianMediaProvider
    redata_provider = "smithsonian"
    display_name = "Smithsonian Open Access"


class LibraryOfCongressMediaProviderTests(_ProviderMediaMappingMixin, SimpleTestCase):
    provider_cls = LibraryOfCongressMediaProvider
    redata_provider = "library_of_congress"
    display_name = "Library of Congress"


class InternetArchiveMediaProviderTests(_ProviderMediaMappingMixin, SimpleTestCase):
    provider_cls = InternetArchiveMediaProvider
    redata_provider = "internet_archive"
    display_name = "Internet Archive"
