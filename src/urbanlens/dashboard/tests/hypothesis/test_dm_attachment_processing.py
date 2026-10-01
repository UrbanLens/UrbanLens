"""A direct-message photo still being re-encoded is a placeholder, never its doomed raw file (P142)."""

from __future__ import annotations

import io
import re
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker
from PIL import Image as PILImage

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.direct_messages.image_permission import DirectMessageImagePermission
from urbanlens.dashboard.models.direct_messages.meta import ImagePermissionStatus
from urbanlens.dashboard.models.direct_messages.model import DirectMessage
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.messaging.direct_messages import serialize_direct_message

_EMPTY_BUBBLE_IMAGE = re.compile(r'<img[^>]*src=""[^>]*dm-bubble__image')
_RAW = "pin_images/rawdm142/upload-dm142.jpg"
_ENCODED = "pin_images/encdm142/encoded-dm142.webp"


def _jpeg_file() -> SimpleUploadedFile:
    buf = io.BytesIO()
    PILImage.new("RGB", (60, 40), color=(10, 20, 30)).save(buf, format="JPEG")
    return SimpleUploadedFile("dm.jpg", buf.getvalue(), content_type="image/jpeg")


class _Conversation(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.sender_user = baker.make(User)
        self.sender = self.sender_user.profile
        self.recipient_user = baker.make(User)
        self.recipient = self.recipient_user.profile
        self.message = baker.make(DirectMessage, sender=self.sender, recipient=self.recipient, body="look")

    def attach(self, *, pending: bool, **fields) -> Image:
        return baker.make(
            Image,
            profile=self.sender,
            direct_message=self.message,
            image=_RAW if pending else _ENCODED,
            pending_scan=pending,
            **fields,
        )

    def allow_images(self) -> None:
        DirectMessageImagePermission.objects.create(
            viewer=self.recipient, sender=self.sender, status=ImagePermissionStatus.ALLOWED
        )


class UploadResponseTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.client.force_login(self.user)

    def test_a_fresh_upload_names_no_file(self) -> None:
        with mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task"):
            response = self.client.post(reverse("messages.upload_image"), {"image": _jpeg_file()})

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertTrue(Image.objects.get(pk=body["id"]).pending_scan)
        self.assertFalse(body.get("url"))
        self.assertIs(body["processing"], True)
        self.assertNotIn("pin_images", response.content.decode())


class PayloadTests(_Conversation):
    def test_the_sender_gets_no_url_for_a_pending_photo(self) -> None:
        image = self.attach(pending=True)

        payload = serialize_direct_message(self.message, viewer=self.sender)

        self.assertEqual(payload["images"], [{"id": image.pk, "processing": True, "processing_failed": False}])

    def test_the_sender_gets_the_url_once_it_is_ready(self) -> None:
        image = self.attach(pending=False)

        payload = serialize_direct_message(self.message, viewer=self.sender)

        self.assertEqual(
            payload["images"],
            [{"id": image.pk, "url": image.image.url, "processing": False, "processing_failed": False}],
        )

    def test_a_consenting_recipient_gets_no_url_for_a_pending_photo(self) -> None:
        self.allow_images()
        image = self.attach(pending=True)

        payload = serialize_direct_message(self.message, viewer=self.recipient)

        self.assertEqual(payload["images"], [{"id": image.pk, "processing": True, "processing_failed": False}])

    def test_a_recipient_without_consent_learns_only_the_id(self) -> None:
        image = self.attach(pending=False)

        payload = serialize_direct_message(self.message, viewer=self.recipient)

        self.assertEqual(payload["images"], [{"id": image.pk}])


class BubbleTests(_Conversation):
    def _thread(self, user: User, partner_slug: str) -> str:
        self.client.force_login(user)
        response = self.client.get(reverse("messages.conversation", args=[partner_slug]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_the_sender_sees_a_polled_placeholder(self) -> None:
        image = self.attach(pending=True)

        html = self._thread(self.sender_user, self.recipient.slug)

        self.assertIn(f'data-id="{image.pk}"', html)
        self.assertIn('data-processing="pending"', html)
        self.assertIn("media-processing", html)
        self.assertIn(f'data-processing-url="{reverse("vault.photos.processing")}"', html)
        self.assertIsNone(_EMPTY_BUBBLE_IMAGE.search(html))
        self.assertNotIn("upload-dm142", html)

    def test_a_consenting_recipient_sees_a_placeholder(self) -> None:
        self.allow_images()
        self.attach(pending=True)

        html = self._thread(self.recipient_user, self.sender.slug)

        self.assertIn("media-processing", html)
        self.assertIsNone(_EMPTY_BUBBLE_IMAGE.search(html))
        self.assertNotIn("upload-dm142", html)

    def test_a_ready_photo_is_an_image(self) -> None:
        image = self.attach(pending=False)

        html = self._thread(self.sender_user, self.recipient.slug)

        self.assertIn(f'src="{image.image.url}"', html)
        self.assertNotIn("media-processing", html)


class RecipientProcessingPollTests(_Conversation):
    """The recipient's placeholder asks the same status endpoint, under the DM's consent rules."""

    def _poll(self, user: User, image: Image) -> dict:
        self.client.force_login(user)
        response = self.client.get(reverse("vault.photos.processing"), {"ids": str(image.pk)})
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_a_consenting_recipient_is_told_it_is_still_processing(self) -> None:
        self.allow_images()
        image = self.attach(pending=True)

        self.assertEqual(self._poll(self.recipient_user, image), {"items": [], "processing": [image.pk]})

    def test_a_consenting_recipient_gets_the_file_urls_once_ready(self) -> None:
        self.allow_images()
        image = self.attach(pending=False)

        body = self._poll(self.recipient_user, image)

        self.assertEqual(body["processing"], [])
        self.assertEqual(len(body["items"]), 1)
        item = body["items"][0]
        self.assertEqual(item["id"], image.pk)
        self.assertEqual(item["url"], image.image.url)
        self.assertIs(item["processing"], False)

    def test_the_recipient_learns_nothing_about_the_photos_other_places(self) -> None:
        self.allow_images()
        pin = baker.make(Pin, profile=self.sender, name="Secret mill", location=baker.make(Location))
        image = self.attach(pending=False, pin=pin, caption="private caption")

        item = self._poll(self.recipient_user, image)["items"][0]

        self.assertLessEqual(set(item), {"id", "url", "thumb_url", "processing", "processing_failed"})
        self.assertNotIn("Secret mill", str(item))

    def test_a_recipient_who_has_not_consented_learns_nothing(self) -> None:
        image = self.attach(pending=True)

        self.assertEqual(self._poll(self.recipient_user, image), {"items": [], "processing": []})

    def test_a_recipient_who_revealed_the_message_once_is_answered(self) -> None:
        DirectMessage.objects.filter(pk=self.message.pk).update(images_revealed=True)
        image = self.attach(pending=True)

        self.assertEqual(self._poll(self.recipient_user, image)["processing"], [image.pk])

    def test_an_outsider_learns_nothing(self) -> None:
        self.allow_images()
        image = self.attach(pending=True)

        self.assertEqual(self._poll(baker.make(User), image), {"items": [], "processing": []})
        Image.objects.filter(pk=image.pk).update(image=_ENCODED, pending_scan=False)
        self.assertEqual(self._poll(baker.make(User), image), {"items": [], "processing": []})

    def test_a_photo_that_failed_is_settled_as_failed(self) -> None:
        self.allow_images()
        image = self.attach(pending=True, upload_failed_at=timezone.now())

        item = self._poll(self.recipient_user, image)["items"][0]

        self.assertIs(item["processing_failed"], True)
        self.assertIsNone(item["url"])
