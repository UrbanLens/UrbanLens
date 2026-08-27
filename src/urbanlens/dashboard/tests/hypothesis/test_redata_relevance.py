"""Tests for services.photos.redata_relevance - wiring photos/votes to REData.

Covers the submission payload builder, the queue_* helpers' REData-not-configured
no-ops, and the two Celery tasks (submit_redata_photos/submit_redata_photo_vote).
Every REData HTTP call is mocked - never hits the network.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, date, datetime, timezone as dt_timezone
from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard import tasks
from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.core.gateway import GatewayRequestError
from urbanlens.dashboard.services.photos import redata_relevance
from urbanlens.UrbanLens.settings.app import settings


@contextmanager
def _redata_configured():
    with mock.patch.object(settings, "redata_api_url", "https://redata.example.test"), mock.patch.object(settings, "redata_api_key", "test-key"):
        yield


@contextmanager
def _redata_not_configured():
    with mock.patch.object(settings, "redata_api_url", None), mock.patch.object(settings, "redata_api_key", None):
        yield


class SubmissionPayloadTests(TestCase):
    def setUp(self) -> None:
        self.location = baker.make(Location, latitude="42.65", longitude="-73.75")

    def test_omits_optional_fields_that_have_no_value(self) -> None:
        image = baker.make(Image, location=self.location, media_type=MediaKind.PHOTO, latitude=None, longitude=None, taken_at=None, profile=None, author="", source_url="")
        payload = redata_relevance._submission_payload(image)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["photo_id"], str(image.uuid))
        self.assertEqual(payload["location_latitude"], 42.65)
        self.assertNotIn("photo_latitude", payload)
        self.assertNotIn("taken_at", payload)
        self.assertNotIn("uploader_id", payload)
        self.assertNotIn("photographer", payload)
        self.assertNotIn("source", payload)

    def test_includes_photo_coordinates_when_both_present(self) -> None:
        image = baker.make(Image, location=self.location, latitude="42.66", longitude="-73.76")
        payload = redata_relevance._submission_payload(image)
        assert payload is not None
        self.assertEqual(payload["photo_latitude"], 42.66)
        self.assertEqual(payload["photo_longitude"], -73.76)

    def test_includes_uploader_id_and_photo_count(self) -> None:
        user = baker.make(User)
        profile = user.profile
        baker.make(Image, location=self.location, profile=profile, _quantity=2)
        image = Image.objects.filter(profile=profile).first()
        payload = redata_relevance._submission_payload(image)
        assert payload is not None
        self.assertEqual(payload["uploader_id"], str(profile.pk))
        self.assertEqual(payload["uploader_photo_count"], 2)

    def test_includes_photographer_from_author_field(self) -> None:
        image = baker.make(Image, location=self.location, author="Jane Doe")
        payload = redata_relevance._submission_payload(image)
        assert payload is not None
        self.assertEqual(payload["photographer"], "Jane Doe")

    def test_source_host_is_extracted_from_source_url(self) -> None:
        image = baker.make(Image, location=self.location, source_url="https://commons.wikimedia.org/wiki/File:Example.jpg")
        payload = redata_relevance._submission_payload(image)
        assert payload is not None
        self.assertEqual(payload["source"], "commons.wikimedia.org")

    def test_years_from_abandoned_uses_wiki_date_abandoned(self) -> None:
        wiki = baker.make(Wiki, location=self.location, date_abandoned=date(2020, 1, 1))
        image = baker.make(Image, location=self.location, wiki=wiki, taken_at=datetime(2022, 1, 1, tzinfo=UTC))
        payload = redata_relevance._submission_payload(image)
        assert payload is not None
        self.assertAlmostEqual(payload["years_from_abandoned"], 2.0, places=1)

    def test_no_usable_coordinates_returns_none(self) -> None:
        image = baker.make(Image, location=None, latitude=None, longitude=None, estimated_latitude=None, estimated_longitude=None)
        self.assertIsNone(redata_relevance._submission_payload(image))


class SubmitPhotosTests(TestCase):
    def setUp(self) -> None:
        self.location = baker.make(Location, latitude="42.65", longitude="-73.75")

    def test_not_configured_does_nothing(self) -> None:
        image = baker.make(Image, location=self.location)
        with _redata_not_configured(), mock.patch("urbanlens.dashboard.services.apis.photos.redata_photos_gateway.RedataPhotosGateway") as gw:
            redata_relevance.submit_photos([image])
        gw.assert_not_called()

    def test_caches_returned_confidence_onto_the_image(self) -> None:
        image = baker.make(Image, location=self.location)
        gateway_instance = mock.Mock()
        gateway_instance.submit_photos.return_value = {"results": {str(image.uuid): {"confidence": 0.73, "scorer": "heuristic", "model_version": None, "scored_at": "2026-08-01T00:00:00Z"}}}
        with _redata_configured(), mock.patch("urbanlens.dashboard.services.apis.photos.redata_photos_gateway.RedataPhotosGateway", return_value=gateway_instance):
            redata_relevance.submit_photos([image])
        image.refresh_from_db()
        self.assertAlmostEqual(image.redata_confidence, 0.73)
        self.assertEqual(image.redata_scorer, "heuristic")
        self.assertIsNone(image.redata_model_version)
        self.assertIsNotNone(image.redata_scored_at)

    def test_skips_images_with_no_usable_location(self) -> None:
        image = baker.make(Image, location=None, latitude=None, longitude=None, estimated_latitude=None, estimated_longitude=None)
        with _redata_configured(), mock.patch("urbanlens.dashboard.services.apis.photos.redata_photos_gateway.RedataPhotosGateway") as gw:
            redata_relevance.submit_photos([image])
        gw.assert_not_called()

    def test_gateway_failure_is_swallowed(self) -> None:
        image = baker.make(Image, location=self.location)
        gateway_instance = mock.Mock()
        gateway_instance.submit_photos.side_effect = GatewayRequestError("boom")
        with _redata_configured(), mock.patch("urbanlens.dashboard.services.apis.photos.redata_photos_gateway.RedataPhotosGateway", return_value=gateway_instance):
            redata_relevance.submit_photos([image])  # must not raise
        image.refresh_from_db()
        self.assertIsNone(image.redata_confidence)


class QueuePhotoSubmissionTests(TestCase):
    def setUp(self) -> None:
        self.location = baker.make(Location)

    def test_not_configured_does_not_enqueue(self) -> None:
        image = baker.make(Image, location=self.location)
        with _redata_not_configured(), mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue:
            redata_relevance.queue_photo_submission(image)
        enqueue.assert_not_called()

    def test_no_location_does_not_enqueue(self) -> None:
        image = baker.make(Image, location=None)
        with _redata_configured(), mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue:
            redata_relevance.queue_photo_submission(image)
        enqueue.assert_not_called()

    def test_configured_with_location_enqueues(self) -> None:
        image = baker.make(Image, location=self.location)
        with _redata_configured(), mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue:
            redata_relevance.queue_photo_submission(image)
        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.args[1], [image.pk])


class QueueRelevanceVoteTests(TestCase):
    def setUp(self) -> None:
        self.location = baker.make(Location)
        self.profile = baker.make(User).profile

    def test_not_configured_does_not_enqueue(self) -> None:
        image = baker.make(Image, location=self.location)
        with _redata_not_configured(), mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue:
            redata_relevance.queue_relevance_vote(image, self.profile, is_relevant=True)
        enqueue.assert_not_called()

    def test_configured_enqueues_with_vote_value(self) -> None:
        image = baker.make(Image, location=self.location)
        with _redata_configured(), mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue:
            redata_relevance.queue_relevance_vote(image, self.profile, is_relevant=False)
        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.args[1:], (image.pk, self.profile.pk, False))


class SubmitRedataPhotosTaskTests(TestCase):
    def test_ignores_non_photo_and_locationless_rows(self) -> None:
        location = baker.make(Location, latitude="42.65", longitude="-73.75")
        video = baker.make(Image, location=location, media_type=MediaKind.VIDEO)
        no_location = baker.make(Image, location=None, media_type=MediaKind.PHOTO)
        with mock.patch("urbanlens.dashboard.services.photos.redata_relevance.submit_photos") as submit:
            result = tasks.submit_redata_photos([video.pk, no_location.pk])
        submit.assert_not_called()
        self.assertFalse(result)

    def test_submits_matching_photo_rows(self) -> None:
        location = baker.make(Location, latitude="42.65", longitude="-73.75")
        photo = baker.make(Image, location=location, media_type=MediaKind.PHOTO)
        with mock.patch("urbanlens.dashboard.services.photos.redata_relevance.submit_photos") as submit:
            result = tasks.submit_redata_photos([photo.pk])
        submit.assert_called_once()
        self.assertEqual([img.pk for img in submit.call_args.args[0]], [photo.pk])
        self.assertTrue(result)


class SubmitRedataPhotoVoteTaskTests(TestCase):
    def setUp(self) -> None:
        self.location = baker.make(Location)
        self.profile = baker.make(User).profile

    def test_missing_image_returns_false(self) -> None:
        result = tasks.submit_redata_photo_vote(999_999, self.profile.pk, is_relevant=True)
        self.assertFalse(result)

    def test_recorded_vote_returns_true(self) -> None:
        image = baker.make(Image, location=self.location)
        gateway_instance = mock.Mock()
        gateway_instance.submit_votes.return_value = {"recorded": 1, "unknown_photo_ids": [], "updated_photos": 1}
        with mock.patch("urbanlens.dashboard.services.apis.photos.redata_photos_gateway.RedataPhotosGateway", return_value=gateway_instance):
            result = tasks.submit_redata_photo_vote(image.pk, self.profile.pk, is_relevant=True)
        self.assertTrue(result)
        vote = gateway_instance.submit_votes.call_args.args[0][0]
        self.assertEqual(vote["photo_id"], str(image.uuid))
        self.assertTrue(vote["is_relevant"])
        self.assertEqual(vote["voter_id"], str(self.profile.pk))

    def test_unknown_photo_id_returns_false(self) -> None:
        image = baker.make(Image, location=self.location)
        gateway_instance = mock.Mock()
        gateway_instance.submit_votes.return_value = {"recorded": 0, "unknown_photo_ids": [str(image.uuid)], "updated_photos": 0}
        with mock.patch("urbanlens.dashboard.services.apis.photos.redata_photos_gateway.RedataPhotosGateway", return_value=gateway_instance):
            result = tasks.submit_redata_photo_vote(image.pk, self.profile.pk, is_relevant=True)
        self.assertFalse(result)

    def test_gateway_failure_returns_false(self) -> None:
        image = baker.make(Image, location=self.location)
        gateway_instance = mock.Mock()
        gateway_instance.submit_votes.side_effect = GatewayRequestError("boom")
        with mock.patch("urbanlens.dashboard.services.apis.photos.redata_photos_gateway.RedataPhotosGateway", return_value=gateway_instance):
            result = tasks.submit_redata_photo_vote(image.pk, self.profile.pk, is_relevant=True)
        self.assertFalse(result)


class RedataConfiguredHelperTests(SimpleTestCase):
    def test_true_only_when_both_url_and_key_set(self) -> None:
        with _redata_configured():
            self.assertTrue(redata_relevance._redata_configured())
        with _redata_not_configured():
            self.assertFalse(redata_relevance._redata_configured())
