"""A uuid-addressed image link that survives the upload's re-encode (P142).

An article embeds its inline images by URL. The upload's raw file is deleted when the re-encode lands,
so the link has to name the row, not the file.
"""

from __future__ import annotations

import io
from pathlib import Path
import shutil
import tempfile
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from model_bakery import baker
from PIL import Image as PILImage

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.direct_messages.model import DirectMessage
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType, Permission
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.auth.api_keys import generate_api_key

_RAW = "pin_images/rawstable/upload-stable.jpg"
_ENCODED = "pin_images/encstable/encoded-stable.webp"


def _jpeg_file(name: str = "inline.jpg") -> SimpleUploadedFile:
    buf = io.BytesIO()
    PILImage.new("RGB", (60, 40), color=(10, 20, 30)).save(buf, format="JPEG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/jpeg")


class _MediaRoot(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._media_root = tempfile.mkdtemp(prefix="ul_stable_link_")
        self.addCleanup(shutil.rmtree, self._media_root, ignore_errors=True)
        overrides = override_settings(MEDIA_ROOT=self._media_root, MEDIA_X_ACCEL=False)
        overrides.enable()
        self.addCleanup(overrides.disable)
        for rel in (_RAW, _ENCODED):
            target = Path(self._media_root) / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"bytes")
        self.owner_user = baker.make(User)
        self.owner = self.owner_user.profile

    def link(self, image: Image) -> str:
        return reverse("media.image", args=[image.uuid])

    def pending(self, **fields) -> Image:
        return baker.make(Image, profile=self.owner, image=_RAW, pending_scan=True, **fields)

    def ready(self, **fields) -> Image:
        return baker.make(Image, profile=self.owner, image=_ENCODED, **fields)

    def key_with(self, scopes: list[ApiKeyScope]) -> str:
        api_key, raw = generate_api_key(self.owner_user, "Media client")
        ApiKey.objects.filter(pk=api_key.pk).update(scopes=[scope.value for scope in scopes])
        return raw

    def befriend_on_shared_wiki(self, image: Image) -> User:
        location = baker.make(Location, latitude=41.7361, longitude=-73.9361)
        Image.objects.filter(pk=image.pk).update(wiki=baker.make(Wiki, location=location))
        friend = baker.make(User)
        Friendship.objects.create(
            from_profile=self.owner,
            to_profile=friend.profile,
            status=FriendshipStatus.ACCEPTED,
            relationship_type=FriendshipType.FRIEND,
            permissions=Permission.VIEW_PROFILE,
        )
        baker.make(Pin, profile=friend.profile, location=location, parent_pin=None)
        return friend


class StableImageLinkAccessTests(_MediaRoot):
    """The link reaches exactly what the media gate would serve for the row's current file."""

    def test_the_owner_is_sent_to_the_current_file(self) -> None:
        image = self.ready()
        self.client.force_login(self.owner_user)

        response = self.client.get(self.link(image))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], image.image.url)
        self.assertIn("no-cache", response["Cache-Control"])
        self.assertIn("private", response["Cache-Control"])

    def test_the_link_follows_the_row_across_the_re_encode(self) -> None:
        image = self.pending()
        link = self.link(image)
        Image.objects.filter(pk=image.pk).update(image=_ENCODED, pending_scan=False)
        self.client.force_login(self.owner_user)

        response = self.client.get(link, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.redirect_chain[0][0], f"/media/{_ENCODED}")

    def test_a_query_string_does_not_change_the_answer(self) -> None:
        image = self.ready()
        self.client.force_login(self.owner_user)

        response = self.client.get(f"{self.link(image)}?r=2")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], image.image.url)

    def test_a_viewer_the_photo_is_shared_with_is_sent_to_it(self) -> None:
        image = self.ready()
        self.client.force_login(self.befriend_on_shared_wiki(image))

        response = self.client.get(self.link(image))

        self.assertEqual(response.status_code, 302)

    def test_another_account_gets_404(self) -> None:
        image = self.ready()
        self.client.force_login(baker.make(User))

        response = self.client.get(self.link(image))

        self.assertEqual(response.status_code, 404)
        self.assertNotIn("Location", response)

    def test_anonymous_is_sent_to_login_and_never_to_the_file(self) -> None:
        image = self.ready()

        response = self.client.get(self.link(image))

        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])
        self.assertNotIn(_ENCODED, response["Location"])

    def test_an_unusable_credential_gets_404(self) -> None:
        image = self.ready()

        response = self.client.get(self.link(image), HTTP_AUTHORIZATION="Bearer ulk_not-a-real-key")

        self.assertEqual(response.status_code, 404)

    def test_a_credential_without_media_read_gets_404(self) -> None:
        image = self.ready()
        raw = self.key_with([ApiKeyScope.PINS_READ])

        response = self.client.get(self.link(image), HTTP_AUTHORIZATION=f"Bearer {raw}")

        self.assertEqual(response.status_code, 404)

    def test_a_credential_with_media_read_is_sent_to_the_file(self) -> None:
        image = self.ready()
        raw = self.key_with([ApiKeyScope.MEDIA_READ])

        response = self.client.get(self.link(image), HTTP_AUTHORIZATION=f"Bearer {raw}")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], image.image.url)

    def test_a_dm_attachment_reaches_only_the_participants(self) -> None:
        recipient = baker.make(User)
        message = baker.make(DirectMessage, sender=self.owner, recipient=recipient.profile)
        image = self.ready(direct_message=message)

        self.client.force_login(recipient)
        self.assertEqual(self.client.get(self.link(image)).status_code, 302)
        self.client.force_login(baker.make(User))
        self.assertEqual(self.client.get(self.link(image)).status_code, 404)

    def test_an_unknown_uuid_gets_404(self) -> None:
        self.client.force_login(self.owner_user)

        response = self.client.get(reverse("media.image", args=["00000000-0000-4000-8000-000000000000"]))

        self.assertEqual(response.status_code, 404)

    def test_a_row_with_no_stored_file_gets_404(self) -> None:
        image = baker.make(Image, profile=self.owner, image="", source_url="https://example.com/p.jpg")
        self.client.force_login(self.owner_user)

        response = self.client.get(self.link(image))

        self.assertEqual(response.status_code, 404)


class StableImageLinkWhilePendingTests(_MediaRoot):
    """While the re-encode is pending the link must not 404 for the uploader, nor name the raw file."""

    def test_the_owner_gets_an_uncached_placeholder(self) -> None:
        image = self.pending()
        self.client.force_login(self.owner_user)

        response = self.client.get(self.link(image))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/svg+xml")
        self.assertIn("no-store", response["Cache-Control"])
        self.assertNotIn(b"upload-stable", response.content)
        self.assertIn(b"<svg", response.content)
        self.assertNotIn(b"<script", response.content.lower())

    def test_the_owner_gets_a_placeholder_for_an_upload_waiting_on_storage(self) -> None:
        from django.utils import timezone

        image = self.pending(upload_failed_at=timezone.now())
        self.client.force_login(self.owner_user)

        response = self.client.get(self.link(image))

        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response["Cache-Control"])

    def test_a_viewer_it_will_be_shared_with_gets_404_until_it_is_ready(self) -> None:
        image = self.pending()
        friend = self.befriend_on_shared_wiki(image)
        self.client.force_login(friend)

        self.assertEqual(self.client.get(self.link(image)).status_code, 404)

        Image.objects.filter(pk=image.pk).update(image=_ENCODED, pending_scan=False)
        self.assertEqual(self.client.get(self.link(image)).status_code, 302)

    def test_another_account_gets_404(self) -> None:
        image = self.pending()
        self.client.force_login(baker.make(User))

        response = self.client.get(self.link(image))

        self.assertEqual(response.status_code, 404)

    def test_a_dm_recipient_gets_404_until_it_is_ready(self) -> None:
        recipient = baker.make(User)
        message = baker.make(DirectMessage, sender=self.owner, recipient=recipient.profile)
        image = self.pending(direct_message=message)
        self.client.force_login(recipient)

        self.assertEqual(self.client.get(self.link(image)).status_code, 404)


class ArticleInlineImageUploadTests(TestCase):
    """The editor writes the upload response's URL into the article body, so it must be the stable link."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.pin = baker.make(Pin, profile=self.profile, name="Mill", name_is_user_provided=True)
        self.client.force_login(self.user)

    def _upload(self):
        with mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task"):
            return self.client.post(reverse("pin.article.image", args=[self.pin.slug]), {"image": _jpeg_file()})

    def test_the_url_names_the_row_not_the_raw_file(self) -> None:
        response = self._upload()

        self.assertEqual(response.status_code, 201)
        body = response.json()
        image = Image.objects.get(pk=body["id"])
        self.assertTrue(image.pending_scan)
        self.assertEqual(body["url"], reverse("media.image", args=[image.uuid]))
        self.assertNotIn("pin_images", body["url"])
        self.assertIs(body["processing"], True)

    def test_the_url_still_resolves_after_the_re_encode_replaces_the_file(self) -> None:
        body = self._upload().json()
        image = Image.objects.get(pk=body["id"])
        raw_name = image.image.name
        image.image.storage.save(_ENCODED, io.BytesIO(b"encoded"))
        Image.objects.filter(pk=image.pk).update(image=_ENCODED, pending_scan=False)
        image.image.storage.delete(raw_name)

        response = self.client.get(body["url"])

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].endswith(_ENCODED))
