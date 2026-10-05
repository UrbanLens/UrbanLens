"""Tests for services.photos.photo_enrichment - background photo/Street View/satellite caching."""

from __future__ import annotations

import io
from itertools import count
import tempfile
from unittest import mock

from django.test import override_settings
from model_bakery import baker
from PIL import Image as PILImage
import requests

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.images.model import Image, ImageSource, MediaKind
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway
from urbanlens.dashboard.services.apis.locations.google.redata_places_gateway import PlacesRateLimitedError
from urbanlens.dashboard.services.apis.locations.places_resolution import PhotoNotFoundError
from urbanlens.dashboard.services.photos import photo_enrichment
from urbanlens.UrbanLens.settings.app import settings as app_settings

_MEDIA_ROOT = tempfile.mkdtemp(prefix="urbanlens-test-media-")
_coordinate_counter = count()

_PLACES_MODULE = "urbanlens.dashboard.services.apis.locations.places_resolution"
_GATEWAY_MODULE = "urbanlens.dashboard.services.apis.locations.google.maps.GoogleMapsGateway"


def _jpeg_bytes(width: int = 64, height: int = 48) -> bytes:
    buf = io.BytesIO()
    PILImage.new("RGB", (width, height), color=(10, 20, 30)).save(buf, format="JPEG")
    return buf.getvalue()


def _make_location() -> Location:
    offset = next(_coordinate_counter)
    return baker.make(
        Location, latitude=f"42.{650_000 + offset}", longitude=f"-73.{760_000 + offset}", google_place=None
    )


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class SaveEnrichedImageTests(TestCase):
    """_save_enriched_image - the shared wiki-attach + quarantine + hand-off helper.

    It is now ``tasks.process_image_upload``'s job, on the sandbox queue, which is why these assertions run the
    task explicitly and re-read the row."""

    def _process(self, image: Image) -> Image:
        """Run the hand-off the enrichment path enqueues, and return the fresh row."""
        from urbanlens.dashboard.tasks import process_image_upload

        process_image_upload(image.pk, 800)
        image.refresh_from_db()
        return image

    def test_creates_wiki_attached_profile_less_image(self) -> None:
        location = _make_location()
        image = photo_enrichment._save_enriched_image(
            location, _jpeg_bytes(1200, 900), source=ImageSource.GOOGLE_MAPS, max_dimension=800
        )
        self.assertEqual(image.location_id, location.pk)
        self.assertIsNotNone(image.wiki_id)
        self.assertIsNone(image.profile_id)
        self.assertIsNone(image.pin_id)
        self.assertEqual(image.source, ImageSource.GOOGLE_MAPS)

    def test_the_row_is_quarantined_until_the_task_has_run(self) -> None:
        # Provider bytes, undecoded. Nothing may show them until they have been
        # scanned and re-encoded - and a profile-less row has no owner to be
        # visible to in the meantime, so this hides it from everyone.
        location = _make_location()
        image = photo_enrichment._save_enriched_image(
            location, _jpeg_bytes(1200, 900), source=ImageSource.GOOGLE_MAPS, max_dimension=800
        )
        self.assertTrue(image.pending_scan)
        self.assertFalse(self._process(image).pending_scan)

    def test_the_task_applies_the_per_source_downscale_cap(self) -> None:
        # The cap travels as an argument because these rows have no profile and
        # therefore no plan policy - without it the task skipped them entirely
        # and the provider's full-size original stayed on disk.
        location = _make_location()
        image = self._process(
            photo_enrichment._save_enriched_image(
                location, _jpeg_bytes(1200, 900), source=ImageSource.GOOGLE_MAPS, max_dimension=800
            )
        )
        self.assertTrue(image.image.name.endswith(".webp"))
        with image.image.open("rb") as fh:
            pil = PILImage.open(fh)
            pil.load()
            self.assertLessEqual(max(pil.size), 800)

    def test_reuses_an_already_existing_wiki(self) -> None:
        location = _make_location()
        wiki = baker.make(Wiki, location=location)
        image = photo_enrichment._save_enriched_image(
            location, _jpeg_bytes(), source=ImageSource.GOOGLE_SATELLITE, max_dimension=800
        )
        self.assertEqual(image.wiki_id, wiki.pk)


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class PlacePhotoEnrichmentSourceTests(TestCase):
    """PlacePhotoEnrichmentSource - Google Places business photo backfill."""

    def setUp(self) -> None:
        self.source = photo_enrichment.PlacePhotoEnrichmentSource()

    def test_gate_requires_google_or_redata(self) -> None:
        with (
            mock.patch.object(app_settings, "google_unrestricted_api_key", ""),
            mock.patch.object(app_settings, "redata_api_url", None),
            mock.patch.object(app_settings, "redata_api_key", None),
        ):
            self.assertFalse(self.source.gate())
        with mock.patch.object(app_settings, "google_unrestricted_api_key", "key"):
            self.assertTrue(self.source.gate())

    def test_service_keys_prefers_redata_when_configured(self) -> None:
        with (
            mock.patch.object(app_settings, "redata_api_url", "https://redata.example.test"),
            mock.patch.object(app_settings, "redata_api_key", "k"),
        ):
            self.assertEqual(self.source.service_keys, ("redata_places",))
        with (
            mock.patch.object(app_settings, "redata_api_url", None),
            mock.patch.object(app_settings, "redata_api_key", None),
        ):
            self.assertEqual(self.source.service_keys, ("google_places",))

    def test_missing_filter_excludes_already_attempted_locations(self) -> None:
        location = _make_location()
        self.assertIn(location, Location.objects.filter(self.source.missing_filter()))
        LocationCache.set(location, self.source.marker_source, {"created": 0, "found": 0})
        self.assertNotIn(location, Location.objects.filter(self.source.missing_filter()))

    def test_enrich_downloads_up_to_the_per_location_cap(self) -> None:
        location = _make_location()
        photo_names = ["a", "b", "c", "d"]
        with (
            mock.patch(f"{_PLACES_MODULE}.find_nearest_place_photos", return_value=("place1", photo_names)),
            mock.patch(f"{_PLACES_MODULE}.download_photo", return_value=(_jpeg_bytes(), "image/jpeg")) as download_mock,
        ):
            result = self.source.enrich(location)

        self.assertTrue(result)
        self.assertEqual(download_mock.call_count, photo_enrichment.MAX_PLACE_PHOTOS)
        images = Image.objects.filter(location=location, media_type=MediaKind.PHOTO)
        self.assertEqual(images.count(), photo_enrichment.MAX_PLACE_PHOTOS)
        for image in images:
            self.assertEqual(image.source, ImageSource.GOOGLE_MAPS)
            self.assertIsNotNone(image.wiki_id)
        marker = LocationCache.objects.get(location=location, source=self.source.marker_source)
        self.assertEqual(marker.data, {"created": photo_enrichment.MAX_PLACE_PHOTOS, "found": len(photo_names)})

    def test_no_photos_found_still_writes_marker_and_creates_nothing(self) -> None:
        location = _make_location()
        with mock.patch(f"{_PLACES_MODULE}.find_nearest_place_photos", return_value=(None, [])):
            result = self.source.enrich(location)

        self.assertTrue(result)
        self.assertFalse(Image.objects.filter(location=location).exists())
        marker = LocationCache.objects.get(location=location, source=self.source.marker_source)
        self.assertEqual(marker.data, {"created": 0, "found": 0})

    def test_one_bad_photo_does_not_abort_the_others(self) -> None:
        location = _make_location()

        def fake_download(photo_name: str, *, api_key: str):
            if photo_name == "bad":
                raise PhotoNotFoundError("gone")
            return _jpeg_bytes(), "image/jpeg"

        with (
            mock.patch(f"{_PLACES_MODULE}.find_nearest_place_photos", return_value=("place1", ["bad", "good"])),
            mock.patch(f"{_PLACES_MODULE}.download_photo", side_effect=fake_download),
        ):
            result = self.source.enrich(location)

        self.assertTrue(result)
        self.assertEqual(Image.objects.filter(location=location).count(), 1)
        marker = LocationCache.objects.get(location=location, source=self.source.marker_source)
        self.assertEqual(marker.data, {"created": 1, "found": 2})

    def test_reuses_a_fresh_google_maps_photos_cache_instead_of_refetching(self) -> None:
        location = _make_location()
        LocationCache.set(
            location, photo_enrichment._PLACE_PHOTO_LIST_CACHE_SOURCE, {"place_id": "p1", "photo_names": ["x"]}
        )
        with (
            mock.patch(f"{_PLACES_MODULE}.find_nearest_place_photos") as find_mock,
            mock.patch(f"{_PLACES_MODULE}.download_photo", return_value=(_jpeg_bytes(), "image/jpeg")),
        ):
            self.source.enrich(location)

        find_mock.assert_not_called()
        self.assertEqual(Image.objects.filter(location=location).count(), 1)

    def test_a_spent_budget_mid_backfill_writes_no_marker(self) -> None:
        """The marker takes the location out of the backfill for good, so a refused download must not write it (P317)."""
        location = _make_location()
        with (
            mock.patch(f"{_PLACES_MODULE}.find_nearest_place_photos", return_value=("place1", ["a", "b"])),
            mock.patch(
                f"{_PLACES_MODULE}.download_photo", side_effect=PlacesRateLimitedError("budget spent", retry_after=600)
            ),
            self.assertRaises(PlacesRateLimitedError),
        ):
            self.source.enrich(location)

        self.assertFalse(LocationCache.objects.filter(location=location, source=self.source.marker_source).exists())
        self.assertIn(location, Location.objects.filter(self.source.missing_filter()))

    def test_an_unreachable_photo_host_writes_no_marker(self) -> None:
        location = _make_location()
        with (
            mock.patch(f"{_PLACES_MODULE}.find_nearest_place_photos", return_value=("place1", ["a"])),
            mock.patch(f"{_PLACES_MODULE}.download_photo", side_effect=requests.ConnectionError("refused")),
            self.assertRaises(requests.ConnectionError),
        ):
            self.source.enrich(location)

        self.assertFalse(LocationCache.objects.filter(location=location, source=self.source.marker_source).exists())

    def test_a_rerun_after_an_outage_saves_no_photo_twice(self) -> None:
        location = _make_location()
        outage = iter([None, PlacesRateLimitedError("budget spent", retry_after=600)])

        def first_run(photo_name: str, *, api_key: str):
            if (failure := next(outage)) is not None:
                raise failure
            return _jpeg_bytes(), "image/jpeg"

        with (
            mock.patch(f"{_PLACES_MODULE}.find_nearest_place_photos", return_value=("place1", ["a", "b", "c"])),
            mock.patch(f"{_PLACES_MODULE}.download_photo", side_effect=first_run),
            self.assertRaises(PlacesRateLimitedError),
        ):
            self.source.enrich(location)
        self.assertEqual(Image.objects.filter(location=location).count(), 1)

        with (
            mock.patch(f"{_PLACES_MODULE}.find_nearest_place_photos") as find_mock,
            mock.patch(f"{_PLACES_MODULE}.download_photo", return_value=(_jpeg_bytes(), "image/jpeg")) as download_mock,
        ):
            self.assertTrue(self.source.enrich(location))

        find_mock.assert_not_called()
        self.assertEqual([call.args[0] for call in download_mock.call_args_list], ["b", "c"])
        self.assertEqual(Image.objects.filter(location=location).count(), 3)
        marker = LocationCache.objects.get(location=location, source=self.source.marker_source)
        self.assertEqual(marker.data, {"created": 3, "found": 3})


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class StreetViewEnrichmentSourceTests(TestCase):
    """StreetViewEnrichmentSource - single static Street View image backfill."""

    def setUp(self) -> None:
        self.source = photo_enrichment.StreetViewEnrichmentSource()

    def test_gate_requires_the_unrestricted_google_key(self) -> None:
        with mock.patch.object(app_settings, "google_unrestricted_api_key", ""):
            self.assertFalse(self.source.gate())
        with mock.patch.object(app_settings, "google_unrestricted_api_key", "key"):
            self.assertTrue(self.source.gate())

    def test_missing_filter_excludes_already_attempted_locations(self) -> None:
        location = _make_location()
        self.assertIn(location, Location.objects.filter(self.source.missing_filter()))
        LocationCache.set(location, self.source.marker_source, {"found": True})
        self.assertNotIn(location, Location.objects.filter(self.source.missing_filter()))

    def test_enrich_saves_an_image_when_coverage_exists(self) -> None:
        location = _make_location()
        with mock.patch(f"{_GATEWAY_MODULE}.get_street_view_single", return_value=(_jpeg_bytes(), "2024-01", 1.0, 2.0)):
            result = self.source.enrich(location)

        self.assertTrue(result)
        image = Image.objects.get(location=location)
        self.assertEqual(image.source, ImageSource.GOOGLE_STREET_VIEW)
        self.assertIsNotNone(image.wiki_id)
        marker = LocationCache.objects.get(location=location, source=self.source.marker_source)
        self.assertTrue(marker.data["found"])

    def test_no_coverage_writes_marker_without_creating_an_image(self) -> None:
        location = _make_location()
        with mock.patch(f"{_GATEWAY_MODULE}.get_street_view_single", side_effect=ValueError("no coverage")):
            result = self.source.enrich(location)

        self.assertTrue(result)
        self.assertFalse(Image.objects.filter(location=location).exists())
        marker = LocationCache.objects.get(location=location, source=self.source.marker_source)
        self.assertFalse(marker.data["found"])

    def test_an_outage_writes_no_marker(self) -> None:
        """The marker is never revisited, so writing it now would mean no Street View for this place, ever (P187)."""
        location = _make_location()
        with (
            mock.patch(f"{_GATEWAY_MODULE}.get_street_view_single", side_effect=requests.ConnectionError("refused")),
            self.assertRaises(requests.ConnectionError),
        ):
            self.source.enrich(location)

        self.assertFalse(LocationCache.objects.filter(location=location, source=self.source.marker_source).exists())


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class SatelliteEnrichmentSourceTests(TestCase):
    """SatelliteEnrichmentSource - single static satellite image backfill."""

    def setUp(self) -> None:
        self.source = photo_enrichment.SatelliteEnrichmentSource()

    def test_enrich_saves_an_image_when_available(self) -> None:
        location = _make_location()
        with mock.patch(f"{_GATEWAY_MODULE}.get_satellite_image_bytes", return_value=_jpeg_bytes()):
            result = self.source.enrich(location)

        self.assertTrue(result)
        image = Image.objects.get(location=location)
        self.assertEqual(image.source, ImageSource.GOOGLE_SATELLITE)
        self.assertIsNotNone(image.wiki_id)
        marker = LocationCache.objects.get(location=location, source=self.source.marker_source)
        self.assertTrue(marker.data["found"])

    def test_unavailable_writes_marker_without_creating_an_image(self) -> None:
        location = _make_location()
        with mock.patch(f"{_GATEWAY_MODULE}.get_satellite_image_bytes", return_value=None):
            result = self.source.enrich(location)

        self.assertTrue(result)
        self.assertFalse(Image.objects.filter(location=location).exists())
        marker = LocationCache.objects.get(location=location, source=self.source.marker_source)
        self.assertFalse(marker.data["found"])

    def test_an_outage_writes_no_marker(self) -> None:
        location = _make_location()
        with (
            mock.patch(f"{_GATEWAY_MODULE}.get_satellite_image_bytes", side_effect=requests.ConnectionError("refused")),
            self.assertRaises(requests.ConnectionError),
        ):
            self.source.enrich(location)

        self.assertFalse(LocationCache.objects.filter(location=location, source=self.source.marker_source).exists())

    def test_a_refused_request_writes_the_marker(self) -> None:
        location = _make_location()
        response = requests.Response()
        response.status_code = 403
        with mock.patch(
            f"{_GATEWAY_MODULE}.get_satellite_image_bytes", side_effect=requests.HTTPError(response=response)
        ):
            self.source.enrich(location)

        marker = LocationCache.objects.get(location=location, source=self.source.marker_source)
        self.assertFalse(marker.data["found"])


class GetSatelliteImageBytesTests(SimpleTestCase):
    """GoogleMapsGateway.get_satellite_image_bytes - the static image the carousel and the backfill both use."""

    def test_returns_the_image_bytes(self) -> None:
        response = mock.Mock(content=b"hello")
        gateway = GoogleMapsGateway(api_key="key", session=mock.Mock(get=mock.Mock(return_value=response)))

        self.assertEqual(gateway.get_satellite_image_bytes(1.0, 2.0), b"hello")

    def test_returns_none_without_a_key(self) -> None:
        session = mock.Mock()
        gateway = GoogleMapsGateway(api_key="", session=session)

        self.assertIsNone(gateway.get_satellite_image_bytes(1.0, 2.0))
        session.get.assert_not_called()

    def test_a_failed_request_raises_for_the_backfill(self) -> None:
        """None would be written down as "no imagery here" by a marker the batch never revisits (P187)."""
        gateway = GoogleMapsGateway(
            api_key="key", session=mock.Mock(get=mock.Mock(side_effect=requests.ConnectionError()))
        )

        with self.assertRaises(requests.ConnectionError):
            gateway.get_satellite_image_bytes(1.0, 2.0)

    def test_the_carousel_still_skips_a_failed_request(self) -> None:
        gateway = GoogleMapsGateway(
            api_key="key", session=mock.Mock(get=mock.Mock(side_effect=requests.ConnectionError()))
        )

        fetch = gateway.get_satellite_slides(1.0, 2.0)

        self.assertEqual(fetch.slides, [])
        self.assertTrue(fetch.degraded, "an unanswered request is not a settled 'no imagery'")
