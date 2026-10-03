"""An upload Garage cannot store is a 503 the client may retry, with nothing left behind (P201).

Garage needs both nodes for a write, and while one is stalled or disconnected it answers 503. That raised out of every
upload view as a 500. Each test here puts default storage on the real S3 backend, built from production's options, and
has the object store refuse the write, so the request goes through botocore's own retries and error parsing.
"""

from __future__ import annotations

import io
import json
import shutil
import tempfile
from typing import TYPE_CHECKING
from unittest import mock

from botocore.exceptions import ClientError
from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import HttpResponse
from django.test import RequestFactory, override_settings
from django.urls import reverse
from model_bakery import baker
from PIL import Image as PILImage

from urbanlens.core.tests.features import grant_alpha_features
from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard import tasks
from urbanlens.dashboard.external_api.errors import uniform_exception_handler
from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.images.issues import PhotoUploadFailure
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.safety.model import SafetyCheckin
from urbanlens.dashboard.models.trips.model import Trip, TripComment, TripMembership
from urbanlens.dashboard.models.visits.model import PinVisit
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.media.storage_errors import STORAGE_RETRY_AFTER_SECONDS
from urbanlens.dashboard.tests.hypothesis.test_object_store_client_config import (
    garage_stalled,
    garage_unavailable,
    object_store,
    writes_fail,
)

if TYPE_CHECKING:
    from django.test.client import _MonkeyPatchedWSGIResponse as TestResponse

_ENQUEUE = "urbanlens.dashboard.services.core.celery.safely_enqueue_task"
_CORNERS = [[42.0, -73.0], [42.0, -72.99], [41.99, -72.99], [41.99, -73.0]]


def _jpeg(name: str = "shot.jpg", colour: tuple[int, int, int] = (10, 20, 30)) -> SimpleUploadedFile:
    buf = io.BytesIO()
    PILImage.new("RGB", (60, 40), color=colour).save(buf, format="JPEG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/jpeg")


def _png(name: str = "icon.png") -> SimpleUploadedFile:
    buf = io.BytesIO()
    PILImage.new("RGBA", (16, 16), color=(200, 10, 10, 255)).save(buf, format="PNG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/png")


def garage_down():
    """Garage refusing every write with its quorum 503."""
    return object_store(writes_fail(garage_unavailable))


class _OutageCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        media_root = tempfile.mkdtemp(prefix="ul_storage_outage_")
        self.addCleanup(shutil.rmtree, media_root, ignore_errors=True)
        media = override_settings(MEDIA_ROOT=media_root)
        media.enable()
        self.addCleanup(media.disable)
        enqueue = mock.patch(_ENQUEUE)
        self.enqueue = enqueue.start()
        self.addCleanup(enqueue.stop)
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.pin = baker.make(Pin, profile=self.profile, location=baker.make(Location))

    def assert_retry_later(self, response: TestResponse) -> None:
        self.assertEqual(response.status_code, 503, response.content[:300])
        self.assertEqual(response["Retry-After"], str(STORAGE_RETRY_AFTER_SECONDS))

    def assert_json_error(self, response: TestResponse) -> None:
        self.assert_retry_later(response)
        self.assertTrue(json.loads(response.content)["error"])

    def assert_text_error(self, response: TestResponse) -> None:
        self.assert_retry_later(response)
        self.assertTrue(response["Content-Type"].startswith("text/"))
        self.assertNotIn(b"<", response.content)
        self.assertTrue(response.content.strip())


class PinGalleryUploadTests(_OutageCase):
    """The pin, wiki and album gallery uploader (``image_gallery.create_uploaded_photo``)."""

    def _post(self, image: SimpleUploadedFile | None = None) -> TestResponse:
        return self.client.post(reverse("pin.gallery", args=[self.pin.slug]), {"image": image or _jpeg()})

    def test_a_write_garage_refuses_is_a_503_with_nothing_stored(self) -> None:
        for fail in (garage_unavailable, garage_stalled):
            with self.subTest(fail=fail.__name__), object_store(writes_fail(fail)):
                response = self._post()
            self.assert_json_error(response)
            self.assertFalse(Image.objects.filter(profile=self.profile).exists())
            self.enqueue.assert_not_called()

    def test_the_failed_upload_is_listed_for_the_owner_to_send_again(self) -> None:
        with garage_down():
            self._post(_jpeg("lighthouse.jpg"))
        self.assertTrue(PhotoUploadFailure.objects.filter(profile=self.profile, filename="lighthouse.jpg").exists())

    def test_sending_it_again_once_garage_is_back_stores_it(self) -> None:
        with garage_down():
            self.assert_json_error(self._post())
        response = self._post()
        self.assertEqual(response.status_code, 201, response.content)
        image = Image.objects.get(profile=self.profile)
        self.assertTrue(default_storage.exists(image.image.name))

    def test_a_write_that_landed_after_the_client_gave_up_is_found_by_the_retry_with_its_file(self) -> None:
        """The store can finish a write the proxy already answered with an error; the row and file land together."""
        self.assertEqual(self._post().status_code, 201)
        response = self._post()
        self.assertEqual(response.status_code, 409, response.content)
        image = Image.objects.get(profile=self.profile)
        self.assertTrue(default_storage.exists(image.image.name))


class PhotoUploadServiceTests(_OutageCase):
    """``photo_upload.upload_photo``: the Vault, the external API, overlays and article sources."""

    def test_the_vault_uploader(self) -> None:
        with garage_down():
            response = self.client.post(reverse("vault.photos.upload"), {"image": _jpeg()})
        self.assert_json_error(response)
        self.assertFalse(Image.objects.filter(profile=self.profile).exists())

    def test_the_external_api_uploader(self) -> None:
        from urbanlens.dashboard.models.account.model import ApiKeyScope
        from urbanlens.dashboard.services.auth.api_keys import generate_api_key

        api_key, raw_key = generate_api_key(self.user, "Test Key")
        api_key.scopes = [ApiKeyScope.PHOTOS_READ.value, ApiKeyScope.PHOTOS_WRITE.value]
        api_key.save(update_fields=["scopes"])
        with garage_down():
            response = self.client.post(
                reverse("external_api:photos"), {"file": _jpeg()}, HTTP_AUTHORIZATION=f"Bearer {raw_key}"
            )
        self.assert_json_error(response)
        self.assertFalse(Image.objects.filter(profile=self.profile).exists())

    def test_the_overlay_uploader(self) -> None:
        with garage_down():
            response = self.client.post(
                reverse("pin.overlays", args=[self.pin.slug]),
                {"corners": json.dumps(_CORNERS), "name": "Sheet", "image": _jpeg()},
            )
        self.assert_text_error(response)
        self.assertFalse(Image.objects.filter(profile=self.profile).exists())


class InlinePhotoUploadTests(_OutageCase):
    """The upload views that store an ``Image`` themselves."""

    def test_the_pin_upload_view(self) -> None:
        with garage_down():
            response = self.client.post(reverse("pin.upload_image", args=[self.pin.slug]), {"image": _jpeg()})
        self.assert_text_error(response)
        self.assertFalse(Image.objects.exists())

    def test_the_article_image_upload(self) -> None:
        with garage_down():
            response = self.client.post(reverse("pin.article.image", args=[self.pin.slug]), {"image": _jpeg()})
        self.assert_json_error(response)
        self.assertFalse(Image.objects.exists())

    def test_the_direct_message_attachment(self) -> None:
        with garage_down():
            response = self.client.post(reverse("messages.upload_image"), {"image": _jpeg()})
        self.assert_json_error(response)
        self.assertFalse(Image.objects.exists())

    def test_the_safety_check_in_gallery(self) -> None:
        checkin = baker.make(SafetyCheckin, profile=self.profile, title="Hike")
        with garage_down():
            response = self.client.post(
                reverse("safety.checkin.gallery", kwargs={"checkin_slug": checkin.slug}), {"image": _jpeg()}
            )
        self.assert_json_error(response)
        self.assertFalse(Image.objects.exists())

    def test_the_photo_scan_suggestion_photo(self) -> None:
        from urbanlens.dashboard.models.pin_suggestions.model import (
            PinSuggestion,
            PinSuggestionOrigin,
            PinSuggestionStatus,
        )

        suggestion = baker.make(
            PinSuggestion,
            profile=self.profile,
            origin=PinSuggestionOrigin.LOCAL_SCAN,
            status=PinSuggestionStatus.PENDING,
        )
        with garage_down():
            response = self.client.post(
                reverse("tools.photo_scan.upload_photo"), {"suggestion_id": str(suggestion.pk), "image": _jpeg()}
            )
        self.assert_json_error(response)
        self.assertFalse(Image.objects.exists())

    def test_the_consensus_round_photo(self) -> None:
        from urbanlens.dashboard.models.consensus.model import (
            ConsensusFieldKind,
            ConsensusRound,
            ConsensusSession,
            ConsensusSessionParticipant,
            ConsensusSessionParticipantStatus,
            ConsensusSessionStatus,
        )

        grant_alpha_features(self.user)
        session = ConsensusSession.objects.create(host_profile=self.profile, status=ConsensusSessionStatus.ACTIVE)
        ConsensusSessionParticipant.objects.create(
            session=session, profile=self.profile, status=ConsensusSessionParticipantStatus.JOINED
        )
        round_ = ConsensusRound.objects.create(
            session=session,
            sequence_index=0,
            wiki=baker.make(Wiki, location=baker.make(Location)),
            field_kind=ConsensusFieldKind.PHOTO_COORDINATES,
        )
        with garage_down():
            response = self.client.post(reverse("consensus.photo", args=[session.pk, round_.pk]), {"image": _jpeg()})
        self.assert_json_error(response)
        self.assertFalse(Image.objects.exists())

    def test_visit_photos_are_refused_and_the_visit_is_still_logged(self) -> None:
        """The visit form logs the visit and warns about each file it could not take, as it does for a full quota."""
        with garage_down():
            response = self.client.post(
                reverse("pin.visits", args=[self.pin.slug]),
                {"visited_date": "2026-09-01", "photos": [_jpeg("a.jpg"), _jpeg("b.jpg", (90, 20, 30))]},
            )
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertTrue(PinVisit.objects.filter(pin=self.pin).exists())
        self.assertFalse(Image.objects.exists())
        self.assertNotIn(
            tasks.process_image_upload, [call.args[0] for call in self.enqueue.call_args_list if call.args]
        )
        warnings = [str(message) for message in get_messages(response.wsgi_request)]
        self.assertTrue(any("Storage" in message for message in warnings), warnings)


class CommentImageTests(_OutageCase):
    """A comment and its photo are stored together or not at all, so sending it again does not post it twice."""

    def test_a_pin_comment(self) -> None:
        with garage_down():
            response = self.client.post(reverse("pin.comments", args=[self.pin.slug]), {"text": "hi", "image": _jpeg()})
        self.assert_text_error(response)
        self.assertFalse(Comment.objects.exists())

    def test_a_trip_comment(self) -> None:
        trip = baker.make(Trip, creator=self.profile, name="Shared Trip")
        TripMembership.objects.create(trip=trip, profile=self.profile, status=TripMembership.STATUS_JOINED)
        with garage_down():
            response = self.client.post(reverse("trips.comments", args=[trip.slug]), {"text": "hi", "image": _jpeg()})
        self.assert_text_error(response)
        self.assertFalse(TripComment.objects.exists())

    def test_a_pin_comment_that_is_stored_is_still_scanned(self) -> None:
        response = self.client.post(reverse("pin.comments", args=[self.pin.slug]), {"text": "hi", "image": _jpeg()})
        self.assertEqual(response.status_code, 200, response.content[:300])
        comment = Comment.objects.get()
        self.assertTrue(comment.pending_scan)
        self.assertTrue(default_storage.exists(comment.image.name))
        self.enqueue.assert_called_once()


class HeldIconAndAvatarTests(_OutageCase):
    """Icons and avatars are held in storage before the worker re-encodes them; holding is the write that fails."""

    def test_a_new_label_with_an_icon_is_not_created(self) -> None:
        with garage_down():
            response = self.client.post(
                reverse("label.create", kwargs={"label_kind": "tag"}), {"name": "Urbex", "custom_icon-new-tag": _png()}
            )
        self.assert_text_error(response)
        self.assertFalse(Label.objects.filter(profile=self.profile, name="Urbex").exists())

    def test_a_new_pin_with_an_icon_is_not_created(self) -> None:
        with garage_down():
            response = self.client.post(
                reverse("pin.add"),
                {"name": "Mill", "latitude": "42.00", "longitude": "-73.50", "custom_icon": _png()},
            )
        self.assert_text_error(response)
        self.assertFalse(Pin.objects.filter(profile=self.profile, name="Mill").exists())

    def test_a_pin_edit_with_an_icon_changes_nothing(self) -> None:
        with garage_down():
            response = self.client.post(
                reverse("pin.quick_edit", args=[self.pin.slug]), {"name": "Renamed", "custom_icon": _png()}
            )
        self.assert_json_error(response)
        self.pin.refresh_from_db()
        self.assertNotEqual(self.pin.name, "Renamed")
        self.assertFalse(self.pin.custom_icon_upload)

    def test_an_avatar_from_the_settings_page(self) -> None:
        with garage_down():
            response = self.client.post(reverse("profile.field.update"), {"field": "avatar", "file_value": _png()})
        self.assert_json_error(response)
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.avatar_upload)

    def test_an_emoji_avatar(self) -> None:
        with garage_down():
            response = self.client.post(
                reverse("profile.field.update"), {"field": "avatar_emoji", "animal": "fox", "color": "#e53935"}
            )
        self.assert_json_error(response)
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.avatar)

    def test_an_avatar_from_the_profile_card_is_refused_with_a_message(self) -> None:
        """A form post: refused the way its other refusals are, with a message on the page it returns to."""
        with garage_down():
            response = self.client.post(reverse("profile.view"), {"avatar": _png()})
        self.assertEqual(response.status_code, 302)
        messages = [str(message) for message in get_messages(response.wsgi_request)]
        self.assertTrue(any("Storage" in message for message in messages), messages)
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.avatar_upload)

    def test_an_avatar_through_the_external_api(self) -> None:
        from urbanlens.dashboard.models.account.model import ApiKeyScope
        from urbanlens.dashboard.services.auth.api_keys import generate_api_key
        from urbanlens.dashboard.tests.hypothesis.test_external_api_social_profile import _multipart_put

        api_key, raw_key = generate_api_key(self.user, "Test Key")
        api_key.scopes = [ApiKeyScope.SOCIAL_READ.value, ApiKeyScope.SOCIAL_WRITE.value, ApiKeyScope.PROFILE_READ.value]
        api_key.save(update_fields=["scopes"])
        url = reverse(
            "external_api:profiles.avatar", kwargs={"profile_slug": self.profile.slug or str(self.profile.uuid)}
        )
        with garage_down():
            response = _multipart_put(self.client, url, {"file": _png()}, {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"})
        self.assert_json_error(response)
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.avatar_upload)

    def test_a_sign_in_whose_avatar_storage_refuses_still_signs_in(self) -> None:
        from urbanlens.dashboard.services.social_auth import pipeline

        with (
            mock.patch.object(pipeline.AvatarService, "resolve_provider_url", return_value="https://example.org/a.png"),
            mock.patch.object(pipeline.AvatarService, "download", return_value=_png().read()),
            garage_down(),
        ):
            pipeline.fetch_and_save_avatar(mock.Mock(name="google"), self.user, {}, is_new=True)
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.avatar_upload)


class EscapedStorageFailureTests(SimpleTestCase):
    """A view that lets a storage failure escape still answers 503, never 500."""

    def _garage_503(self) -> ClientError:
        return ClientError(
            {"Error": {"Code": "ServiceUnavailable"}, "ResponseMetadata": {"HTTPStatusCode": 503}}, "PutObject"
        )

    def test_the_middleware_answers_a_raw_object_store_failure(self) -> None:
        from urbanlens.dashboard.middleware import StorageUnavailableMiddleware

        middleware = StorageUnavailableMiddleware(lambda request: HttpResponse())
        request = RequestFactory().post("/anything/")
        response = middleware.process_exception(request, self._garage_503())
        assert response is not None
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response["Retry-After"], str(STORAGE_RETRY_AFTER_SECONDS))
        self.assertIsNone(middleware.process_exception(request, ValueError("not storage")))

    def test_the_middleware_is_installed(self) -> None:
        from django.conf import settings

        self.assertIn("urbanlens.dashboard.middleware.StorageUnavailableMiddleware", settings.MIDDLEWARE)

    def test_the_external_api_answers_a_raw_object_store_failure_in_its_envelope(self) -> None:
        response = uniform_exception_handler(self._garage_503(), {})
        assert response is not None
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response["Retry-After"], str(STORAGE_RETRY_AFTER_SECONDS))
        self.assertTrue(response.data["error"])


class EscapedHeldUploadTests(_OutageCase):
    def test_a_label_edit_with_an_icon_reaches_the_middleware_as_a_503(self) -> None:
        label = baker.make(Label, profile=self.profile, kind="tag", name="Old")
        with garage_down():
            response = self.client.post(
                reverse("label.edit", kwargs={"label_kind": "tag", "label_id": label.pk}),
                {"name": "New", "custom_icon": _png()},
            )
        self.assert_text_error(response)
        label.refresh_from_db()
        self.assertEqual(label.name, "Old")
