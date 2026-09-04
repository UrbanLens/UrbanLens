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
from urbanlens.dashboard.models.images.model import Image, ImageSource
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.services.memories.journal import JournalEntry

if TYPE_CHECKING:
    from collections.abc import Iterable

#: A real 1x1 PNG - ImageField stores whatever bytes it's given, but the
#: upload pipeline sniffs content, so a valid file avoids testing the wrong
#: rejection path.
_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

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
                response = getattr(self.client, method)(
                    url, payload or None, content_type="application/json", **_bearer(self.raw_key)
                )
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


class PhotoDeleteDualOwnershipTests(_PhotoApiTestCase):
    """A photo that's also linked to a wiki (``wiki_creation._seed_photos`` and
    ``PinGalleryBulkView``'s "send to wiki" repoint the row rather than copying
    it) must not be destroyed just because the mobile client deleted it from
    the caller's own photo library - the web `PinImageView`/`WikiImageView`
    guard the same case (see test_pin_wiki_image_dual_ownership.py); this API
    is a second, independent surface hitting the identical unconditional
    `image.delete()` bug."""

    def setUp(self) -> None:
        super().setUp()
        self.location = Location.objects.create(latitude=41.0, longitude=-71.0)
        self.pin = Pin.objects.create(profile=self.profile, location=self.location, name="Mobile dual-owned spot")
        self.wiki = Wiki.objects.create(location=self.location)

    def test_deleting_a_dual_owned_photo_unlinks_the_pin_and_keeps_the_wiki_copy(self) -> None:
        image = _make_image(self.profile, pin=self.pin, wiki=self.wiki, location=self.location)
        url = reverse("external_api:photos.detail", kwargs={"image_uuid": image.uuid})

        response = self.client.delete(url, **_bearer(self.raw_key))

        self.assertEqual(response.status_code, HTTPStatus.NO_CONTENT)
        self.assertTrue(Image.objects.filter(pk=image.pk, wiki=self.wiki, pin__isnull=True).exists())

    def test_asking_withdraws_a_dual_owned_photo_entirely(self) -> None:
        """?from_wiki=true must win even when a pin is still attached - the
        pin-preserving unlink the default case does above must not silently
        swallow an explicit request to withdraw from the wiki too."""
        image = _make_image(
            self.profile, pin=self.pin, wiki=self.wiki, location=self.location, source=ImageSource.UPLOAD
        )
        url = reverse("external_api:photos.detail", kwargs={"image_uuid": image.uuid})

        response = self.client.delete(f"{url}?from_wiki=true", **_bearer(self.raw_key))

        self.assertEqual(response.status_code, HTTPStatus.NO_CONTENT)
        self.assertFalse(Image.objects.filter(pk=image.pk).exists())


class PhotoDeleteAndTheWikiTests(_PhotoApiTestCase):
    """Deleting over the API withdraws a wiki contribution only if asked.

    The same rule the pin gallery follows: contributing a photo to a community
    wiki is a deliberate act, so undoing it is another one, and a caller that
    says nothing gets the answer that needs no action. A client has what it needs
    to ask first - ``wiki_slug`` and ``source`` are both on the photo payload.
    """

    def setUp(self) -> None:
        super().setUp()
        from urbanlens.dashboard.models.wiki.model import Wiki

        self.wiki = baker.make(
            Wiki, location=self.image.location or baker.make("dashboard.Location", latitude=41.7, longitude=-73.9)
        )
        Image.objects.filter(pk=self.image.pk).update(wiki=self.wiki, source=ImageSource.UPLOAD)
        self.image.refresh_from_db()

    def _url(self) -> str:
        return reverse("external_api:photos.detail", kwargs={"image_uuid": self.image.uuid})

    def test_a_silent_delete_leaves_it_on_the_wiki(self) -> None:
        response = self.client.delete(self._url(), **_bearer(self.raw_key))

        self.assertEqual(response.status_code, HTTPStatus.NO_CONTENT)
        self.assertTrue(
            Image.objects.filter(pk=self.image.pk, wiki=self.wiki).exists(),
            "the API withdrew a wiki contribution nobody asked to withdraw",
        )

    def test_asking_withdraws_it(self) -> None:
        response = self.client.delete(f"{self._url()}?from_wiki=true", **_bearer(self.raw_key))

        self.assertEqual(response.status_code, HTTPStatus.NO_CONTENT)
        self.assertFalse(Image.objects.filter(pk=self.image.pk).exists())

    def test_an_external_photo_stays_even_when_asked(self) -> None:
        """A fetched photo was public online before we saw it - nothing to withdraw."""
        Image.objects.filter(pk=self.image.pk).update(source=ImageSource.LINKED_URL)

        response = self.client.delete(f"{self._url()}?from_wiki=true", **_bearer(self.raw_key))

        self.assertEqual(response.status_code, HTTPStatus.NO_CONTENT)
        self.assertTrue(
            Image.objects.filter(pk=self.image.pk, wiki=self.wiki).exists(),
            "an external photo was pulled off the wiki by an API delete",
        )

    def test_a_photo_on_no_wiki_still_deletes_outright(self) -> None:
        """The ordinary case, unchanged."""
        Image.objects.filter(pk=self.image.pk).update(wiki=None)

        response = self.client.delete(self._url(), **_bearer(self.raw_key))

        self.assertEqual(response.status_code, HTTPStatus.NO_CONTENT)
        self.assertFalse(Image.objects.filter(pk=self.image.pk).exists())


class PhotoLabelTests(_PhotoApiTestCase):
    """Label replacement is scoped to the caller's own media labels."""

    def test_labels_are_created_and_replaced(self) -> None:
        """PUT sets exactly the submitted names, and a second PUT replaces them."""
        url = reverse("external_api:photos.labels", kwargs={"image_uuid": self.image.uuid})

        response = self.client.put(
            url, {"labels": ["Rooftop", "Night"]}, content_type="application/json", **_bearer(self.raw_key)
        )
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


class JournalResponseShapeTests(TestCase):
    """The journal answers with the external API's standard paginated envelope.

    Regression coverage for the bare ``{entries,total,omitted_sources}`` shape this
    endpoint used to answer with - it could never gain a field later without
    breaking clients, so it was normalized onto ``{count,next,previous,results}``
    (see ``docs/notes/mobile_app_notes.md`` Part 7).
    """

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)

    def _make_pin(self, name: str, lat: float, lng: float):
        location = baker.make("dashboard.Location", latitude=lat, longitude=lng)
        return baker.make("dashboard.Pin", profile=self.profile, location=location, name=name)

    def test_envelope_has_standard_keys_plus_omitted_sources(self) -> None:
        """A partially-scoped credential gets the standard envelope, with
        omitted_sources naming the journal sources it can't see."""
        pin = self._make_pin("Old Factory", 40.0, -73.0)
        baker.make("dashboard.Review", profile=self.profile, pin=pin, rating=4)
        raw_key = _key_with_scopes(self.user, [ApiKeyScope.PHOTOS_READ, ApiKeyScope.PINS_READ])

        response = self.client.get(reverse("external_api:memories.journal"), **_bearer(raw_key))

        self.assertEqual(response.status_code, HTTPStatus.OK)
        body = response.json()
        self.assertEqual(set(body), {"count", "next", "previous", "results", "omitted_sources"})
        self.assertEqual(body["count"], 1)
        self.assertEqual(len(body["results"]), 1)
        self.assertEqual(body["results"][0]["kind"], "review")
        self.assertCountEqual(body["omitted_sources"], ["visits", "comments", "articles"])

    def test_fully_scoped_credential_omits_nothing(self) -> None:
        raw_key = _key_with_scopes(
            self.user,
            [
                ApiKeyScope.PHOTOS_READ,
                ApiKeyScope.PINS_READ,
                ApiKeyScope.VISITS_READ,
                ApiKeyScope.WIKI_READ,
                ApiKeyScope.TRIPS_READ,
            ],
        )

        response = self.client.get(reverse("external_api:memories.journal"), **_bearer(raw_key))

        self.assertEqual(response.json()["omitted_sources"], [])

    def test_pages_via_the_standard_page_size_param(self) -> None:
        """?page_size= pages the merged feed exactly like every other list endpoint."""
        for i in range(2):
            pin = self._make_pin(f"Pin {i}", 41.0 + i, -74.0 - i)
            baker.make("dashboard.Review", profile=self.profile, pin=pin, rating=3)
        raw_key = _key_with_scopes(self.user, [ApiKeyScope.PHOTOS_READ, ApiKeyScope.PINS_READ])

        first = self.client.get(reverse("external_api:memories.journal"), {"page_size": 1}, **_bearer(raw_key)).json()
        self.assertEqual(first["count"], 2)
        self.assertEqual(len(first["results"]), 1)
        self.assertIsNotNone(first["next"])
        self.assertIsNone(first["previous"])

        second = self.client.get(
            reverse("external_api:memories.journal"), {"page_size": 1, "page": 2}, **_bearer(raw_key)
        ).json()
        self.assertEqual(len(second["results"]), 1)
        self.assertIsNone(second["next"])
        self.assertIsNotNone(second["previous"])
        self.assertNotEqual(first["results"][0]["title"], second["results"][0]["title"])


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

    @patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task")
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

    @patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task")
    def test_duplicate_upload_is_refused(self, _mock_enqueue) -> None:
        """The same bytes twice is a 409, matching the web uploader."""
        for _ in range(2):
            upload = SimpleUploadedFile("dupe.png", _PNG_BYTES, content_type="image/png")
            response = self.client.post(reverse("external_api:photos"), {"file": upload}, **_bearer(self.raw_key))
        self.assertEqual(response.status_code, HTTPStatus.CONFLICT)
