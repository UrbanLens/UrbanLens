"""Guards on the external API's photo, suggestion, journal and media-file surface.

The invariants here are the ones that fail silently and expensively:

1. **No silent grant expansion.** The photo scopes were added to
   ``ApiKeyScope`` *after* keys were already in the wild, and
   ``_default_api_key_scopes()`` was deliberately not widened to include them.
   A key issued before this feature must therefore be refused by every
   endpoint added with it - if someone ever "helpfully" backfills the new
   scopes onto existing rows, these tests are what notices.
2. **Writes are owner-scoped, not visibility-scoped.** ``visible_to`` includes
   friends' and community photos the caller may look at but must never delete,
   relabel, re-file or vote through. Every write endpoint resolves its photo by
   ``profile__user`` instead, and answers 404 (never 403) for someone else's.
3. **The media gate honors credentials without weakening authorization.** A
   credential holding ``media:read`` resolves to a profile and then walks the
   identical policy a session walks; one without the scope gets nothing.
4. **The journal contract can't silently lose a field.** ``JournalEntry`` is a
   dataclass and ``JournalEntrySerializer`` mirrors it by hand, so a new
   dataclass field would otherwise just stop reaching clients.
"""

from __future__ import annotations

import base64
import dataclasses
from http import HTTPStatus
from pathlib import Path
import tempfile
from typing import TYPE_CHECKING
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from model_bakery import baker

from urbanlens.dashboard.external_api.serializers import JournalEntrySerializer
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.api_keys import generate_api_key
from urbanlens.dashboard.services.memories.journal import JournalEntry

if TYPE_CHECKING:
    from collections.abc import Iterable

#: A real 1x1 PNG - ImageField stores whatever bytes it's given, but the
#: upload pipeline sniffs content, so a valid file avoids testing the wrong
#: rejection path.
_PNG_BYTES = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

#: Everything this change added, as (method, url-name) - used to assert that a
#: key without the new scopes reaches none of it.
_NEW_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("get", "external_api:photos"),
    ("post", "external_api:photos"),
    ("get", "external_api:photos.detail"),
    ("delete", "external_api:photos.detail"),
    ("put", "external_api:photos.labels"),
    ("post", "external_api:photos.vote"),
    ("post", "external_api:photos.file"),
    ("get", "external_api:suggestions.visits"),
    ("get", "external_api:memories.journal"),
)


def _bearer(raw_key: str) -> dict:
    """Build the Authorization header kwargs for a raw API key."""
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


def _key_with_scopes(user: User, scopes: Iterable[ApiKeyScope]) -> str:
    """Issue an API key for *user* carrying exactly *scopes*, returning the raw key."""
    api_key, raw_key = generate_api_key(user, "Test Key")
    api_key.scopes = [scope.value for scope in scopes]
    api_key.save(update_fields=["scopes"])
    return raw_key


def _make_image(profile: Profile, **kwargs) -> Image:
    """Create an Image row owned by *profile* with a real stored file."""
    return Image.objects.create(
        image=SimpleUploadedFile("photo.png", _PNG_BYTES, content_type="image/png"),
        profile=profile,
        **kwargs,
    )


class _PhotoApiTestCase(TestCase):
    """Shared fixture: an owner with a photo, plus an unrelated second user."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.other_user = baker.make(User)
        self.other_profile = Profile.objects.get(user=self.other_user)

        self.raw_key = _key_with_scopes(self.user, [ApiKeyScope.PHOTOS_READ, ApiKeyScope.PHOTOS_WRITE])
        self.image = _make_image(self.profile)
        self.other_image = _make_image(self.other_profile)


class ExistingKeyGrantTests(_PhotoApiTestCase):
    """A key issued before the photo scopes existed must reach none of this."""

    def test_default_scoped_key_is_refused_on_every_new_endpoint(self) -> None:
        """The original four-scope grant gets 403 everywhere, not partial access."""
        _api_key, legacy_raw = generate_api_key(self.user, "Legacy")

        for method, name in _NEW_ENDPOINTS:
            with self.subTest(endpoint=f"{method.upper()} {name}"):
                url = reverse(name, kwargs={"image_uuid": self.image.uuid}) if "photos." in name else reverse(name)
                response = getattr(self.client, method)(url, **_bearer(legacy_raw))
                self.assertEqual(response.status_code, HTTPStatus.FORBIDDEN)

    def test_suggestion_action_is_also_refused(self) -> None:
        """The suggestion accept/dismiss route is covered by the same refusal."""
        _api_key, legacy_raw = generate_api_key(self.user, "Legacy")
        url = reverse("external_api:suggestions.visits.action", kwargs={"suggestion_id": 1, "action": "accept"})
        response = self.client.post(url, **_bearer(legacy_raw))
        self.assertEqual(response.status_code, HTTPStatus.FORBIDDEN)


class PhotoScopeSeparationTests(_PhotoApiTestCase):
    """photos:read must not confer photos:write."""

    def test_read_only_key_cannot_write(self) -> None:
        """Every mutating photo endpoint refuses a read-only key."""
        read_only = _key_with_scopes(self.user, [ApiKeyScope.PHOTOS_READ])
        cases = (
            ("delete", reverse("external_api:photos.detail", kwargs={"image_uuid": self.image.uuid})),
            ("put", reverse("external_api:photos.labels", kwargs={"image_uuid": self.image.uuid})),
            ("post", reverse("external_api:photos.vote", kwargs={"image_uuid": self.image.uuid})),
            ("post", reverse("external_api:photos.file", kwargs={"image_uuid": self.image.uuid})),
        )
        for method, url in cases:
            with self.subTest(url=url):
                response = getattr(self.client, method)(url, **_bearer(read_only))
                self.assertEqual(response.status_code, HTTPStatus.FORBIDDEN)

    def test_read_only_key_can_read(self) -> None:
        """The same key is accepted on the read endpoints."""
        read_only = _key_with_scopes(self.user, [ApiKeyScope.PHOTOS_READ])
        response = self.client.get(reverse("external_api:photos"), **_bearer(read_only))
        self.assertEqual(response.status_code, HTTPStatus.OK)


class ForeignPhotoTests(_PhotoApiTestCase):
    """Another user's photo is indistinguishable from one that doesn't exist."""

    def test_every_endpoint_returns_404_for_another_users_photo(self) -> None:
        """Read and write endpoints alike answer 404, never 403."""
        cases = (
            ("get", "external_api:photos.detail", {}),
            ("delete", "external_api:photos.detail", {}),
            ("put", "external_api:photos.labels", {"labels": ["x"]}),
            ("post", "external_api:photos.vote", {"value": 1}),
            ("post", "external_api:photos.file", {}),
        )
        for method, name, payload in cases:
            with self.subTest(endpoint=f"{method.upper()} {name}"):
                url = reverse(name, kwargs={"image_uuid": self.other_image.uuid})
                response = getattr(self.client, method)(url, payload or None, content_type="application/json", **_bearer(self.raw_key))
                self.assertEqual(response.status_code, HTTPStatus.NOT_FOUND)

    def test_deleting_another_users_photo_leaves_it_intact(self) -> None:
        """The 404 is not a 'deleted anyway' - the row survives."""
        url = reverse("external_api:photos.detail", kwargs={"image_uuid": self.other_image.uuid})
        self.client.delete(url, **_bearer(self.raw_key))
        self.assertTrue(Image.objects.filter(pk=self.other_image.pk).exists())

    def test_listing_never_includes_another_users_photo(self) -> None:
        """The list endpoint is scoped to the caller's own uploads."""
        response = self.client.get(reverse("external_api:photos"), **_bearer(self.raw_key))
        self.assertEqual(response.status_code, HTTPStatus.OK)
        returned = {row["uuid"] for row in response.json()["results"]}
        self.assertIn(str(self.image.uuid), returned)
        self.assertNotIn(str(self.other_image.uuid), returned)


class PhotoDeleteTests(_PhotoApiTestCase):
    """The caller's own photo deletes cleanly."""

    def test_owner_can_delete_their_photo(self) -> None:
        """A 204 and the row is gone."""
        url = reverse("external_api:photos.detail", kwargs={"image_uuid": self.image.uuid})
        response = self.client.delete(url, **_bearer(self.raw_key))
        self.assertEqual(response.status_code, HTTPStatus.NO_CONTENT)
        self.assertFalse(Image.objects.filter(pk=self.image.pk).exists())


class PhotoLabelTests(_PhotoApiTestCase):
    """Label replacement is scoped to the caller's own media labels."""

    def test_labels_are_created_and_replaced(self) -> None:
        """PUT sets exactly the submitted names, and a second PUT replaces them."""
        url = reverse("external_api:photos.labels", kwargs={"image_uuid": self.image.uuid})

        response = self.client.put(url, {"labels": ["Rooftop", "Night"]}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, HTTPStatus.OK)
        self.assertEqual(sorted(response.json()["labels"]), ["Night", "Rooftop"])

        response = self.client.put(url, {"labels": ["Night"]}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.json()["labels"], ["Night"])

    def test_labels_are_media_kind_and_owned_by_the_caller(self) -> None:
        """A created label is a media label belonging to the submitting profile."""
        url = reverse("external_api:photos.labels", kwargs={"image_uuid": self.image.uuid})
        self.client.put(url, {"labels": ["Drone"]}, content_type="application/json", **_bearer(self.raw_key))

        label = self.image.labels.get()
        self.assertEqual(label.kind, "media")
        self.assertEqual(label.profile_id, self.profile.pk)


class PhotoVoteTests(_PhotoApiTestCase):
    """Votes are refused for photos with no gallery identity."""

    def test_plain_upload_cannot_be_voted_on(self) -> None:
        """A personal upload has no (source, item_key) identity - 400, not 500."""
        url = reverse("external_api:photos.vote", kwargs={"image_uuid": self.image.uuid})
        response = self.client.post(url, {"value": 1}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, HTTPStatus.BAD_REQUEST)


class JournalContractTests(SimpleTestCase):
    """The journal serializer must mirror the JournalEntry dataclass exactly."""

    def test_serializer_fields_match_dataclass_fields(self) -> None:
        """A field added to JournalEntry must be added to the serializer too."""
        dataclass_fields = {field.name for field in dataclasses.fields(JournalEntry)}
        serializer_fields = set(JournalEntrySerializer().fields)
        self.assertEqual(dataclass_fields, serializer_fields)


@override_settings(MEDIA_X_ACCEL=False)
class MediaGateCredentialTests(TestCase):
    """The media gate accepts credentials with media:read, and nothing else."""

    def setUp(self) -> None:
        self.tempdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self._media_override = override_settings(MEDIA_ROOT=self.tempdir)
        self._media_override.enable()
        self.addCleanup(self._media_override.disable)

        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.image = _make_image(self.profile)
        self.media_url = reverse("media", kwargs={"path": self.image.image.name})

    def _cleanup(self) -> None:
        import shutil

        shutil.rmtree(self.tempdir, ignore_errors=True)

    def test_credential_with_media_read_is_served(self) -> None:
        """A key holding media:read fetches its owner's own file."""
        raw_key = _key_with_scopes(self.user, [ApiKeyScope.MEDIA_READ])
        response = self.client.get(self.media_url, **_bearer(raw_key))
        self.assertEqual(response.status_code, HTTPStatus.OK)

    def test_credential_without_media_read_gets_404(self) -> None:
        """A key lacking media:read is refused, and cannot tell the file exists."""
        raw_key = _key_with_scopes(self.user, [ApiKeyScope.PHOTOS_READ])
        response = self.client.get(self.media_url, **_bearer(raw_key))
        self.assertEqual(response.status_code, HTTPStatus.NOT_FOUND)

    def test_credential_cannot_reach_another_users_file(self) -> None:
        """media:read resolves who is asking; it does not widen what they may see."""
        stranger = baker.make(User)
        raw_key = _key_with_scopes(stranger, [ApiKeyScope.MEDIA_READ])
        response = self.client.get(self.media_url, **_bearer(raw_key))
        self.assertEqual(response.status_code, HTTPStatus.NOT_FOUND)

    def test_anonymous_browser_request_still_redirects_to_login(self) -> None:
        """The existing browser UX is unchanged for a request with no credential."""
        response = self.client.get(self.media_url)
        self.assertEqual(response.status_code, HTTPStatus.FOUND)
        self.assertIn("/login", response["Location"].lower())

    def test_session_login_still_serves_the_file(self) -> None:
        """A logged-in session reaches its own file exactly as before."""
        self.client.force_login(self.user)
        response = self.client.get(self.media_url)
        self.assertEqual(response.status_code, HTTPStatus.OK)

    def test_bad_credential_gets_404_not_a_login_redirect(self) -> None:
        """An API-shaped request is never bounced to an HTML login form."""
        response = self.client.get(self.media_url, HTTP_AUTHORIZATION="Bearer ulk_not_a_real_key")
        self.assertEqual(response.status_code, HTTPStatus.NOT_FOUND)


class PhotoUploadApiTests(TestCase):
    """Uploading through the API goes through the shared pipeline."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.raw_key = _key_with_scopes(self.user, [ApiKeyScope.PHOTOS_READ, ApiKeyScope.PHOTOS_WRITE])

    @patch("urbanlens.dashboard.services.celery.safely_enqueue_task")
    def test_upload_creates_a_photo(self, mock_enqueue) -> None:
        """A multipart upload lands as an Image owned by the key's user."""
        upload = SimpleUploadedFile("new.png", _PNG_BYTES, content_type="image/png")
        response = self.client.post(
            reverse("external_api:photos"),
            {"file": upload, "caption": "From the API"},
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, HTTPStatus.CREATED, response.content)
        body = response.json()
        self.assertEqual(body["caption"], "From the API")

        image = Image.objects.get(uuid=body["uuid"])
        self.assertEqual(image.profile_id, self.profile.pk)
        mock_enqueue.assert_called_once()

    @patch("urbanlens.dashboard.services.celery.safely_enqueue_task")
    def test_duplicate_upload_is_refused(self, _mock_enqueue) -> None:
        """The same bytes twice is a 409, matching the web uploader."""
        for _ in range(2):
            upload = SimpleUploadedFile("dupe.png", _PNG_BYTES, content_type="image/png")
            response = self.client.post(reverse("external_api:photos"), {"file": upload}, **_bearer(self.raw_key))
        self.assertEqual(response.status_code, HTTPStatus.CONFLICT)
