"""Tests for the REData media gateway and the street-view carousel providers.

The three providers (Mapillary, KartaView, Panoramax) now source their slides
from REData's ``/street-view/timeline/`` - one dated slide per capture date,
representative frame nearest the point - rather than ``/media/lookup/``'s
undated recent photos. ``RedataMediaGateway.lookup`` itself remains (the
``/media/lookup/`` contract still serves other consumers), so its tests stay.

Mirrors ``test_redata_context_gateway.py``'s conventions: a mocked ``session``
for the gateway-level tests (no DB, no network), and a mocked
``RedataStreetViewGateway.get_timeline`` for the provider-level slide-mapping
tests.
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
from urbanlens.dashboard.services.apis.locations.redata_street_view_gateway import RedataStreetViewGateway

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

        results = _gateway(session).lookup(
            38.456, -77.123, kind="photo", provider="mapillary", radius_meters=50, limit=5
        )

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


class RedataStreetViewGatewayTimelineTests(SimpleTestCase):
    def test_requests_the_timeline_path_with_provider(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, {"dates": [], "years": [], "providers": []})

        gateway = RedataStreetViewGateway(base_url="https://redata.example.test", api_key="test-key", session=session)
        body = gateway.get_timeline(38.456, -77.123, provider="mapillary")

        self.assertEqual(body["dates"], [])
        url = session.get.call_args.args[0]
        self.assertEqual(url, "https://redata.example.test/api/v1/street-view/timeline/")
        params = session.get.call_args.kwargs["params"]
        self.assertEqual(params["provider"], "mapillary")


def _timeline(dates: list[dict]) -> dict:
    return {"dates": dates, "years": [], "earliest": None, "latest": None, "providers_timeline": [], "providers": []}


def _date_entry(captured_on: str, **representative: object) -> dict:
    return {"captured_on": captured_on, "count": 3, "is_panoramic": False, "representative": representative}


class _ProviderSlideMappingMixin(_MixinBase):
    """Shared assertions for each thin street-view provider, run against a
    mocked ``RedataStreetViewGateway.get_timeline`` so these stay pure unit tests."""

    provider_cls: type[StreetViewProvider]
    redata_provider: str
    display_name: str

    def _slides(self, dates: list[dict]) -> tuple[list[StreetViewSlide], mock.Mock]:
        # __post_init__ is neutralised alongside get_timeline because the
        # provider builds its own RedataStreetViewGateway() from the
        # module-level settings, and that constructor raises unless
        # UL_REDATA_API_URL/API_KEY happen to be set in the environment
        # running the tests. These are pure slide-mapping assertions - what
        # they must not depend on is whether the machine has REData
        # credentials configured.
        with (
            mock.patch.object(RedataStreetViewGateway, "__post_init__", return_value=None),
            mock.patch.object(RedataStreetViewGateway, "get_timeline", return_value=_timeline(dates)) as mock_timeline,
        ):
            slides = list(self.provider_cls()._generate_street_view_slides(38.456, -77.123, radius=50))
        return slides, mock_timeline

    def test_requests_its_own_provider_tag(self) -> None:
        _slides, mock_timeline = self._slides([])
        mock_timeline.assert_called_once_with(38.456, -77.123, provider=self.redata_provider)

    def test_maps_representative_url_heading_coordinates_and_date(self) -> None:
        dates = [
            _date_entry(
                "2019-06-01",
                image_url="https://example.test/full.jpg",
                thumbnail_url="https://example.test/thumb.jpg",
                heading_degrees=87.5,
                latitude=38.1,
                longitude=-77.2,
            ),
        ]
        slides, _ = self._slides(dates)
        self.assertEqual(len(slides), 1)
        slide = slides[0]
        self.assertEqual(slide.img_src, "https://example.test/full.jpg")
        self.assertEqual(slide.source, self.display_name)
        self.assertEqual(slide.heading, 87.5)
        self.assertEqual(slide.latitude, 38.1)
        self.assertEqual(slide.longitude, -77.2)
        self.assertEqual(slide.date, "2019-06-01")

    def test_yields_newest_capture_date_first(self) -> None:
        """The decay progression starts from the most recent look at the site."""
        dates = [
            _date_entry("2015-04-02", image_url="https://example.test/old.jpg"),
            _date_entry("2023-09-14", image_url="https://example.test/new.jpg"),
        ]
        slides, _ = self._slides(dates)
        self.assertEqual([slide.date for slide in slides], ["2023-09-14", "2015-04-02"])

    def test_falls_back_to_thumbnail_url_when_image_url_missing(self) -> None:
        slides, _ = self._slides([_date_entry("2020-01-01", thumbnail_url="https://example.test/thumb.jpg")])
        self.assertEqual(slides[0].img_src, "https://example.test/thumb.jpg")

    def test_skips_dates_whose_representative_has_no_url(self) -> None:
        slides, _ = self._slides([_date_entry("2020-01-01")])
        self.assertEqual(slides, [])

    def test_missing_heading_is_none(self) -> None:
        slides, _ = self._slides([_date_entry("2020-01-01", image_url="https://example.test/a.jpg")])
        self.assertIsNone(slides[0].heading)

    def test_service_key_is_the_historical_per_provider_tag(self) -> None:
        self.assertEqual(self.provider_cls.service_key, self.redata_provider)

    def test_a_gateway_failure_propagates_rather_than_being_swallowed(self) -> None:
        """The street-view carousel's own collector already tolerates one
        provider raising (see ``collect_street_view_slides``) - this provider
        must not duplicate that handling by swallowing the error itself."""
        with (
            mock.patch.object(RedataStreetViewGateway, "__post_init__", return_value=None),
            mock.patch.object(
                RedataStreetViewGateway,
                "get_timeline",
                side_effect=LocationContextUnavailableError("source_error", "down"),
            ),
            pytest.raises(LocationContextUnavailableError),
        ):
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
