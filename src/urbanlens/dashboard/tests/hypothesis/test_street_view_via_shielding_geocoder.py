"""With a shielding geocoder available, Street View coverage, Street View and satellite backfill go to it and not to Google with this deployment's key (UL-352)."""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.plugins.points import GEOCODERS
from urbanlens.dashboard.services.apis.locations.geocode_resolution import (
    GeocodeProvider,
    StreetViewCoverage,
    StreetViewPicture,
    ask_shielding_geocoder,
)
from urbanlens.dashboard.services.core.gateway import ProviderUnavailableError
from urbanlens.dashboard.services.core.rate_limiter import RateLimitExceededError
from urbanlens.dashboard.services.photos import photo_enrichment
from urbanlens.dashboard.tests.hypothesis.test_photo_enrichment import _MEDIA_ROOT, _jpeg_bytes

_GOOGLE_MAPS = "urbanlens.dashboard.services.apis.locations.google.maps.GoogleMapsGateway"
_GOOGLE_METADATA = "urbanlens.dashboard.controllers.maps.GoogleStreetViewMetadataGateway"
_PERMITTED = "urbanlens.dashboard.services.core.egress.direct_fallback_permitted"
_KEY = "urbanlens.UrbanLens.settings.app.settings.google_unrestricted_api_key"


class _Shield(GeocodeProvider):
    """A shielding provider answering the map-service lookups with what it was given, or raising ``failure``."""

    key = "shield"
    shields_direct = True

    def __init__(
        self,
        *,
        coverage: StreetViewCoverage | None = None,
        picture: StreetViewPicture | None = None,
        satellite: bytes | None = None,
        failure: Exception | None = None,
    ) -> None:
        self.coverage = coverage
        self.picture = picture
        self.satellite = satellite
        self.failure = failure

    def available(self) -> bool:
        return True

    def street_view_service_keys(self) -> tuple[str, ...]:
        return ("shield_street_view",)

    def satellite_service_keys(self) -> tuple[str, ...]:
        return ("shield_imagery",)

    def geocode(self, address: str) -> tuple[float | None, float | None]:
        return None, None

    def street_view_coverage(self, latitude: float, longitude: float) -> StreetViewCoverage | None:
        if self.failure is not None:
            raise self.failure
        return self.coverage

    def street_view_picture(self, latitude: float, longitude: float) -> StreetViewPicture | None:
        if self.failure is not None:
            raise self.failure
        return self.picture

    def satellite_picture(self, latitude: float, longitude: float) -> bytes | None:
        if self.failure is not None:
            raise self.failure
        return self.satellite


class _ShieldedCase(TestCase):
    def shield(self, provider: _Shield) -> None:
        override = GEOCODERS.overridden([provider])
        override.__enter__()
        self.addCleanup(override.__exit__, None, None, None)


class StreetViewCheckTests(_ShieldedCase):
    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        baker.make(User)  # the first user is auto-promoted to site admin
        self.client.force_login(baker.make(User))

    def check(self) -> dict:
        response = self.client.get(reverse("map.streetview_check"), {"lat": "40.7", "lng": "-74.0"})
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_coverage_comes_from_the_shield_without_a_google_key(self) -> None:
        self.shield(_Shield(coverage=StreetViewCoverage(available=True, pano_id="p")))
        with mock.patch(_KEY, ""), mock.patch(_GOOGLE_METADATA) as google:
            self.assertEqual(self.check(), {"available": True})
        google.assert_not_called()

    def test_no_coverage_is_not_asked_of_google_again(self) -> None:
        self.shield(_Shield(coverage=StreetViewCoverage(available=False)))
        with mock.patch(_KEY, "key"), mock.patch(_GOOGLE_METADATA) as google:
            self.assertEqual(self.check(), {"available": False})
        google.assert_not_called()

    def test_off_production_an_outage_is_an_error_and_not_google(self) -> None:
        self.shield(_Shield(failure=ProviderUnavailableError("down")))
        with (
            mock.patch(_KEY, "key"),
            mock.patch(_PERMITTED, return_value=False),
            mock.patch(_GOOGLE_METADATA) as google,
        ):
            self.assertEqual(self.check(), {"available": False, "reason": "error"})
        google.assert_not_called()

    def test_on_production_an_outage_falls_back_to_google(self) -> None:
        self.shield(_Shield(failure=ProviderUnavailableError("down")))
        with mock.patch(_KEY, "key"), mock.patch(_PERMITTED, return_value=True), mock.patch(_GOOGLE_METADATA) as google:
            google.return_value.has_imagery.return_value = True
            self.assertEqual(self.check(), {"available": True})

    def test_a_right_click_storm_on_one_spot_asks_the_shield_once(self) -> None:
        shield = _Shield(coverage=StreetViewCoverage(available=True, pano_id="p"))
        self.shield(shield)
        with mock.patch.object(shield, "street_view_coverage", wraps=shield.street_view_coverage) as asked:
            for _ in range(5):
                self.assertEqual(self.check(), {"available": True})
        self.assertEqual(asked.call_count, 1)

    def test_a_refusal_by_the_limiter_is_refused_not_a_server_error(self) -> None:
        self.shield(_Shield(failure=RateLimitExceededError("shield_street_view", "limit")))
        self.assertEqual(self.check(), {"available": False, "reason": "refused"})

    def test_an_outage_is_not_cached(self) -> None:
        shield = _Shield(failure=ProviderUnavailableError("down"))
        self.shield(shield)
        with mock.patch(_PERMITTED, return_value=False):
            self.check()
            shield.failure = None
            shield.coverage = StreetViewCoverage(available=True)
            self.assertEqual(self.check(), {"available": True})


class UnansweredQuestionTests(_ShieldedCase):
    def test_a_provider_that_does_not_implement_a_lookup_was_not_asked(self) -> None:
        class _Geocoder(_Shield):
            def street_view_coverage(self, latitude: float, longitude: float) -> StreetViewCoverage | None:
                raise NotImplementedError

        self.shield(_Geocoder())
        self.assertEqual(
            ask_shielding_geocoder(lambda provider: provider.street_view_coverage(1.0, 2.0)), (False, None)
        )


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class NoFallbackKeyTests(_ShieldedCase):
    """On production with the shield down and no Google key, the question goes unasked and no marker retires the location."""

    def _run(
        self, source: photo_enrichment.StreetViewEnrichmentSource | photo_enrichment.SatelliteEnrichmentSource
    ) -> Location:
        self.shield(_Shield(failure=ProviderUnavailableError("down")))
        location = baker.make(Location, latitude="42.333333", longitude="-73.333333", google_place=None)
        with (
            mock.patch(_PERMITTED, return_value=True),
            mock.patch(_KEY, ""),
            self.assertRaises(ProviderUnavailableError),
        ):
            source.enrich(location)
        return location

    def test_street_view_writes_no_marker(self) -> None:
        source = photo_enrichment.StreetViewEnrichmentSource()
        location = self._run(source)
        self.assertFalse(LocationCache.objects.filter(location=location, source=source.marker_source).exists())

    def test_satellite_writes_no_marker(self) -> None:
        source = photo_enrichment.SatelliteEnrichmentSource()
        location = self._run(source)
        self.assertFalse(LocationCache.objects.filter(location=location, source=source.marker_source).exists())


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class StreetViewBackfillTests(_ShieldedCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = photo_enrichment.StreetViewEnrichmentSource()
        self.location: Location = baker.make(Location, latitude="42.111111", longitude="-73.111111", google_place=None)

    def test_it_runs_on_the_shield_without_a_google_key(self) -> None:
        self.shield(_Shield())
        with mock.patch(_KEY, ""):
            self.assertTrue(self.source.gate())
        self.assertEqual(self.source.service_keys, ("shield_street_view",))

    def test_the_picture_comes_from_the_shield_and_not_google(self) -> None:
        self.shield(_Shield(picture=StreetViewPicture(content=_jpeg_bytes(), pano_id="p")))
        with mock.patch(_GOOGLE_MAPS) as google:
            self.assertTrue(self.source.enrich(self.location))
        google.assert_not_called()
        self.assertEqual(Image.objects.filter(location=self.location).count(), 1)
        self.assertTrue(
            LocationCache.objects.get(location=self.location, source=self.source.marker_source).data["found"]
        )

    def test_no_panorama_settles_the_marker_without_an_image(self) -> None:
        self.shield(_Shield(picture=None))
        with mock.patch(_GOOGLE_MAPS) as google:
            self.assertTrue(self.source.enrich(self.location))
        google.assert_not_called()
        self.assertFalse(Image.objects.filter(location=self.location).exists())
        self.assertFalse(
            LocationCache.objects.get(location=self.location, source=self.source.marker_source).data["found"]
        )

    def test_an_outage_writes_no_marker(self) -> None:
        self.shield(_Shield(failure=ProviderUnavailableError("down")))
        with mock.patch(_PERMITTED, return_value=False), self.assertRaises(ProviderUnavailableError):
            self.source.enrich(self.location)
        self.assertFalse(
            LocationCache.objects.filter(location=self.location, source=self.source.marker_source).exists()
        )


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class SatelliteBackfillTests(_ShieldedCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = photo_enrichment.SatelliteEnrichmentSource()
        self.location: Location = baker.make(Location, latitude="42.222222", longitude="-73.222222", google_place=None)

    def test_it_runs_on_the_shield_without_a_google_key(self) -> None:
        self.shield(_Shield())
        with mock.patch(_KEY, ""):
            self.assertTrue(self.source.gate())
        self.assertEqual(self.source.service_keys, ("shield_imagery",))

    def test_the_picture_comes_from_the_shield_and_not_google(self) -> None:
        self.shield(_Shield(satellite=_jpeg_bytes()))
        with mock.patch(_GOOGLE_MAPS) as google:
            self.assertTrue(self.source.enrich(self.location))
        google.assert_not_called()
        self.assertEqual(Image.objects.filter(location=self.location).count(), 1)

    def test_on_production_an_outage_falls_back_to_google(self) -> None:
        self.shield(_Shield(failure=ProviderUnavailableError("down")))
        with mock.patch(_PERMITTED, return_value=True), mock.patch(_KEY, "key"), mock.patch(_GOOGLE_MAPS) as google:
            google.return_value.get_satellite_image_bytes.return_value = _jpeg_bytes()
            self.assertTrue(self.source.enrich(self.location))
        self.assertEqual(Image.objects.filter(location=self.location).count(), 1)

    def test_without_a_shield_the_source_is_google_as_before(self) -> None:
        with GEOCODERS.overridden([]), mock.patch(_KEY, ""):
            self.assertFalse(self.source.gate())
            self.assertEqual(self.source.service_keys, ("google_maps",))
