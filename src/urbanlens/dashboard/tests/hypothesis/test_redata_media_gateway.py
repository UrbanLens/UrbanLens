"""Tests for the REData-backed street-view carousel providers (Mapillary,
KartaView, Panoramax) - ``services.apis.locations.redata_media_gateway``.

Mirrors ``test_redata_context_gateway.py``'s conventions: a mocked ``session``
for the gateway-level tests (no DB, no network), and a mocked
``RedataMediaGateway.lookup`` for the provider-level slide-mapping tests, since
those only care about turning a result dict into a ``StreetViewSlide``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import mock

import pytest

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError
from urbanlens.dashboard.services.apis.locations.redata_media_gateway import (
    KartaViewStreetViewProvider,
    MapillaryStreetViewProvider,
    PanoramaxStreetViewProvider,
    RedataMediaGateway,
)

if TYPE_CHECKING:
    from urbanlens.dashboard.services.apis.locations.base import StreetViewProvider, StreetViewSlide

    # Gives the mixin's own methods real assertX()/provider_cls typing under
    # mypy without unittest actually discovering and running _ProviderSlideMappingMixin
    # on its own (it has no provider_cls/redata_provider/display_name set) -
    # unittest.TestCase subclasses are collected by class regardless of name,
    # unlike plain classes, so the real base must stay `object` at runtime.
    _MixinBase = SimpleTestCase
else:
    _MixinBase = object


def _response(status_code: int, body: object) -> mock.Mock:
    resp = mock.Mock(status_code=status_code)
    resp.json.return_value = body
    resp.text = ""
    return resp


def _gateway(session: mock.Mock) -> RedataMediaGateway:
    return RedataMediaGateway(base_url="https://redata.example.test", api_key="test-key", session=session)


class RedataMediaGatewayLookupTests(SimpleTestCase):
    def test_sends_kind_and_provider_and_returns_results(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(
            200,
            {
                "count": 1,
                "complete": True,
                "results": [{"provider": "mapillary", "url": "https://example.test/a.jpg"}],
                "providers": [],
            },
        )

        results = _gateway(session).lookup(38.456, -77.123, kind="photo", provider="mapillary", radius_meters=50, limit=5)

        self.assertEqual(results, [{"provider": "mapillary", "url": "https://example.test/a.jpg"}])
        params = session.get.call_args.kwargs["params"]
        self.assertEqual(params["kind"], "photo")
        self.assertEqual(params["provider"], "mapillary")
        self.assertEqual(params["radius_meters"], 50)
        self.assertEqual(params["limit"], 5)
        url = session.get.call_args.args[0]
        self.assertEqual(url, "https://redata.example.test/api/v1/media/lookup/")

    def test_omits_kind_when_not_given(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, {"count": 0, "complete": True, "results": [], "providers": []})

        _gateway(session).lookup(1.0, 2.0)

        params = session.get.call_args.kwargs["params"]
        self.assertNotIn("kind", params)

    def test_unavailable_propagates(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(503, {"error": "all_providers_unavailable", "message": "down"})

        with pytest.raises(LocationContextUnavailableError):
            _gateway(session).lookup(1.0, 2.0, kind="photo", provider="mapillary")


class _ProviderSlideMappingMixin(_MixinBase):
    """Shared assertions for each thin street-view provider, run against a
    mocked ``RedataMediaGateway.lookup`` so these stay pure unit tests."""

    provider_cls: type[StreetViewProvider]
    redata_provider: str
    display_name: str

    def _slides(self, results: list[dict]) -> tuple[list[StreetViewSlide], mock.Mock]:
        with mock.patch.object(RedataMediaGateway, "lookup", return_value=results) as mock_lookup:
            slides = list(self.provider_cls()._generate_street_view_slides(38.456, -77.123, radius=50))
        return slides, mock_lookup

    def test_requests_photo_kind_and_its_own_provider_tag(self) -> None:
        _slides, mock_lookup = self._slides([])
        mock_lookup.assert_called_once_with(38.456, -77.123, kind="photo", provider=self.redata_provider, radius_meters=50, limit=5)

    def test_maps_url_heading_and_coordinates(self) -> None:
        results = [
            {
                "url": "https://example.test/full.jpg",
                "thumbnail_url": "https://example.test/thumb.jpg",
                "attributes": {"heading_degrees": 87.5, "family": "street_level"},
                "latitude": 38.1,
                "longitude": -77.2,
                "record_retrieved_at": "2026-07-01T00:00:00Z",
            },
        ]
        slides, _ = self._slides(results)
        self.assertEqual(len(slides), 1)
        slide = slides[0]
        self.assertEqual(slide.img_src, "https://example.test/full.jpg")
        self.assertEqual(slide.source, self.display_name)
        self.assertEqual(slide.heading, 87.5)
        self.assertEqual(slide.latitude, 38.1)
        self.assertEqual(slide.longitude, -77.2)
        self.assertEqual(slide.date, "2026-07-01")

    def test_falls_back_to_thumbnail_url_when_url_missing(self) -> None:
        slides, _ = self._slides([{"thumbnail_url": "https://example.test/thumb.jpg"}])
        self.assertEqual(slides[0].img_src, "https://example.test/thumb.jpg")

    def test_skips_items_with_no_url_at_all(self) -> None:
        slides, _ = self._slides([{"attributes": {}}])
        self.assertEqual(slides, [])

    def test_missing_heading_is_none(self) -> None:
        slides, _ = self._slides([{"url": "https://example.test/a.jpg", "attributes": {}}])
        self.assertIsNone(slides[0].heading)

    def test_missing_capture_date_falls_back_to_unknown(self) -> None:
        slides, _ = self._slides([{"url": "https://example.test/a.jpg"}])
        self.assertEqual(slides[0].date, "Unknown")

    def test_service_key_is_the_historical_per_provider_tag(self) -> None:
        self.assertEqual(self.provider_cls.service_key, self.redata_provider)

    def test_a_gateway_failure_propagates_rather_than_being_swallowed(self) -> None:
        """The street-view carousel's own collector already tolerates one
        provider raising (see ``collect_street_view_slides``) - this provider
        must not duplicate that handling by swallowing the error itself."""
        with mock.patch.object(RedataMediaGateway, "lookup", side_effect=LocationContextUnavailableError("source_error", "down")), pytest.raises(LocationContextUnavailableError):
            list(self.provider_cls()._generate_street_view_slides(38.456, -77.123))


class MapillaryStreetViewProviderTests(_ProviderSlideMappingMixin, SimpleTestCase):
    provider_cls = MapillaryStreetViewProvider
    redata_provider = "mapillary"
    display_name = "Mapillary"


class KartaViewStreetViewProviderTests(_ProviderSlideMappingMixin, SimpleTestCase):
    provider_cls = KartaViewStreetViewProvider
    redata_provider = "kartaview"
    display_name = "KartaView"


class PanoramaxStreetViewProviderTests(_ProviderSlideMappingMixin, SimpleTestCase):
    provider_cls = PanoramaxStreetViewProvider
    redata_provider = "panoramax"
    display_name = "Panoramax"
