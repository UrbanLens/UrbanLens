"""External API: avatar writes and the private profile annotations.

Two surfaces that existed only as HTML before this change, and one recurring
rule they share: **404, never 403, and never a distinguishable one.**

The avatar routes may only ever touch the caller's own profile, so every other
slug answers exactly as an unknown slug does - a 403 would confirm that some
other account owns that slug. They are gated on ``social:write`` rather than
``photos:write`` on purpose: an avatar creates no ``Image`` row and consumes no
photo quota, while ``photos:write`` would additionally authorize deleting the
user's actual photographs.

The annotation routes carry the sharper rule. ``ProfileNickname`` and
``ProfileTrust`` are private *to their author*: the person being annotated must
never be able to read what someone else recorded about them. The tests below
assert that from the subject's side explicitly, because a queryset filtered on
``subject`` alone rather than through ``for_pair(author=viewer, ...)`` would
pass every author-side test in this file while handing the subject everyone's
private opinion of them.
"""

from __future__ import annotations

import base64
import tempfile
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.test.client import BOUNDARY, MULTIPART_CONTENT, encode_multipart
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.colors import MaterialColor
from urbanlens.dashboard.models.profile.meta import VisibilityChoice
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.profile.nickname import ProfileNickname
from urbanlens.dashboard.models.profile.note import ProfileNote
from urbanlens.dashboard.models.profile.trust import ProfileTrust
from urbanlens.dashboard.services.auth.api_keys import generate_api_key

#: A real 1x1 PNG. The upload pipeline sniffs magic bytes, so junk content would
#: exercise the content-mismatch rejection instead of the path under test.
_PNG_BYTES = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

#: A file whose magic bytes identify it as a PDF - used to prove the sniffing
#: step runs. Junk bytes would not do: a format ``filetype`` cannot fingerprint
#: is trusted rather than rejected, so only an identifiable *mismatch* fails.
_PDF_BYTES = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"


def _bearer(raw_key: str) -> dict:
    """Build the Authorization header kwargs for the test client.

    Args:
        raw_key: The plaintext API key.

    Returns:
        Kwargs to splat into a test-client call.
    """
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


def _key_with_scopes(user: User, *scopes: ApiKeyScope) -> str:
    """Issue an API key granting exactly ``scopes``.

    Args:
        user: The key's owner.
        scopes: The scopes to grant.

    Returns:
        The plaintext key.
    """
    api_key, raw_key = generate_api_key(user, "Test")
    api_key.scopes = [scope.value for scope in scopes]
    api_key.save(update_fields=["scopes"])
    return raw_key


class _SocialProfileTestCase(TestCase):
    """A key owner and a second, unrelated account, both fully scoped."""

    def setUp(self) -> None:
        """Create the caller, a second account, and their API keys."""
        baker.make(User)  # first user is auto-promoted to bootstrap site admin
        self.user = baker.make(User, username="owner")
        self.profile = Profile.objects.get(user=self.user)
        self.other_user = baker.make(User, username="bystander")
        self.other = Profile.objects.get(user=self.other_user)
        # ``profile_visibility`` defaults to ANYTHING_IN_COMMON, and two fresh
        # accounts share no pin, friend or trip - so without this the profiles
        # would be invisible to each other and every test here would 404 on the
        # visibility gate rather than exercising the behaviour it names.
        # ``test_invisible_profile_is_404_on_every_route`` re-tightens it.
        for profile in (self.profile, self.other):
            profile.profile_visibility = VisibilityChoice.ANYONE
            profile.save(update_fields=["profile_visibility"])
        self.raw_key = _key_with_scopes(self.user, ApiKeyScope.SOCIAL_READ, ApiKeyScope.SOCIAL_WRITE, ApiKeyScope.PROFILE_READ)
        self.other_key = _key_with_scopes(self.other_user, ApiKeyScope.SOCIAL_READ, ApiKeyScope.SOCIAL_WRITE, ApiKeyScope.PROFILE_READ)

    def _slug(self, profile: Profile) -> str:
        """The path segment addressing a profile.

        Args:
            profile: The profile to address.

        Returns:
            Its slug, falling back to its uuid.
        """
        return profile.slug or str(profile.uuid)


#: Uploads must not land in the checkout's real media tree. Held as a module
#: -level ``TemporaryDirectory`` so its finalizer removes the tree when the
#: process exits, rather than each test class racing to delete a shared path.
_MEDIA_TMP = tempfile.TemporaryDirectory(prefix="ul-avatar-tests-")
_MEDIA_ROOT = _MEDIA_TMP.name


def _multipart_put(client, url: str, files: dict, headers: dict):
    """PUT a multipart body through Django's test client.

    ``Client.put`` sends the body verbatim rather than encoding it the way
    ``Client.post`` does, so a dict handed to it arrives as a stringified dict
    and ``request.FILES`` comes back empty - which looks exactly like the
    "no file provided" path and would quietly turn a real assertion into a
    tautology.

    Args:
        client: The Django test client.
        url: The target URL.
        files: Mapping of field name to file object.
        headers: Extra request kwargs (the bearer token).

    Returns:
        The test-client response.
    """
    return client.put(url, encode_multipart(BOUNDARY, files), content_type=MULTIPART_CONTENT, **headers)


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class AvatarUploadTests(_SocialProfileTestCase):
    """PUT/DELETE ``/profiles/{slug}/avatar/``."""

    def _url(self, profile: Profile) -> str:
        """The avatar route for a profile.

        Args:
            profile: The profile named in the path.

        Returns:
            The reversed URL.
        """
        return reverse("external_api:profiles.avatar", kwargs={"profile_slug": self._slug(profile)})

    def _put_png(self, profile: Profile, raw_key: str | None = None):
        """PUT a valid PNG at a profile's avatar route.

        Args:
            profile: The profile named in the path.
            raw_key: Credential to use; defaults to the fixture owner's.

        Returns:
            The test-client response.
        """
        return _multipart_put(
            self.client,
            self._url(profile),
            {"file": SimpleUploadedFile("avatar.png", _PNG_BYTES, content_type="image/png")},
            _bearer(raw_key or self.raw_key),
        )

    def test_put_stores_the_avatar_and_returns_the_profile(self) -> None:
        """A valid upload lands on the model and comes back in the payload."""
        response = self._put_png(self.profile)

        self.assertEqual(response.status_code, 200)
        self.profile.refresh_from_db()
        self.assertTrue(self.profile.avatar)
        self.assertTrue(response.json()["avatar_url"])

    def test_put_on_another_profile_is_indistinguishable_from_an_unknown_slug(self) -> None:
        """Never 403: a refusal must not confirm the slug belongs to somebody."""
        refused = self._put_png(self.other)
        unknown = _multipart_put(
            self.client,
            reverse("external_api:profiles.avatar", kwargs={"profile_slug": "no-such-person"}),
            {"file": SimpleUploadedFile("avatar.png", _PNG_BYTES, content_type="image/png")},
            _bearer(self.raw_key),
        )

        self.assertEqual(refused.status_code, 404)
        self.assertEqual(refused.status_code, unknown.status_code)
        self.assertEqual(refused.content, unknown.content)
        self.other.refresh_from_db()
        self.assertFalse(self.other.avatar)

    def test_put_without_a_file_is_400(self) -> None:
        """A well-formed body carrying no file part is a client error, not a 500."""
        response = _multipart_put(self.client, self._url(self.profile), {"unused": "1"}, _bearer(self.raw_key))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"error": "No file provided."})

    def test_content_that_is_not_an_image_is_refused_and_stores_nothing(self) -> None:
        """Magic-byte sniffing runs here exactly as it does on the web form.

        The payload is a real PDF wearing a ``.png`` name and an ``image/png``
        Content-Type. Unrecognizable junk would *not* be rejected - a format
        ``filetype`` cannot fingerprint is deliberately trusted (see
        ``services.security.content_sniffing``) - so a mismatch only exists when the
        bytes are identifiable as something else.
        """
        response = _multipart_put(
            self.client,
            self._url(self.profile),
            {"file": SimpleUploadedFile("avatar.png", _PDF_BYTES, content_type="image/png")},
            _bearer(self.raw_key),
        )

        self.assertEqual(response.status_code, 400)
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.avatar)

    def test_upload_refusals_keep_their_own_status_and_message(self) -> None:
        """The shared ``image_upload_error`` vocabulary is passed through verbatim.

        A client needs one mapping for uploads, not one per endpoint - and the
        503 in particular must stay distinguishable from a 400, because it is
        the only one worth retrying.
        """
        cases = (
            ("This file is too large.", 413),
            ("Our antivirus scanner is temporarily unavailable. Please try again shortly.", 503),
        )
        for message, status_code in cases:
            with self.subTest(status=status_code), patch("urbanlens.dashboard.services.media.images.image_upload_error", return_value=(message, status_code)):
                response = self._put_png(self.profile)
                self.assertEqual(response.status_code, status_code)
                self.assertEqual(response.json(), {"error": message})

    def test_put_requires_social_write(self) -> None:
        """``photos:write`` must not reach this route, and read-only must not either."""
        for scope in (ApiKeyScope.SOCIAL_READ, ApiKeyScope.PHOTOS_WRITE):
            with self.subTest(scope=scope.value):
                raw_key = _key_with_scopes(self.user, scope)
                response = self._put_png(self.profile, raw_key=raw_key)
                self.assertEqual(response.status_code, 403)

    def test_delete_clears_the_avatar_and_is_idempotent(self) -> None:
        """A retried DELETE still succeeds, so a timed-out mobile call is safe to replay."""
        self._put_png(self.profile)

        first = self.client.delete(self._url(self.profile), **_bearer(self.raw_key))
        second = self.client.delete(self._url(self.profile), **_bearer(self.raw_key))

        self.assertEqual(first.status_code, 204)
        self.assertEqual(second.status_code, 204)
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.avatar)

    def test_delete_on_another_profile_is_404_and_leaves_their_avatar(self) -> None:
        """Someone else's avatar is not deletable, and the refusal reveals nothing."""
        self.other.avatar = SimpleUploadedFile("theirs.png", _PNG_BYTES, content_type="image/png")
        self.other.save(update_fields=["avatar"])

        response = self.client.delete(self._url(self.other), **_bearer(self.raw_key))

        self.assertEqual(response.status_code, 404)
        self.other.refresh_from_db()
        self.assertTrue(self.other.avatar)


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class AvatarEmojiTests(_SocialProfileTestCase):
    """POST ``/profiles/{slug}/avatar/emoji/``."""

    def _url(self, profile: Profile) -> str:
        """The emoji-avatar route for a profile.

        Args:
            profile: The profile named in the path.

        Returns:
            The reversed URL.
        """
        return reverse("external_api:profiles.avatar.emoji", kwargs={"profile_slug": self._slug(profile)})

    def _post(self, profile: Profile, payload: dict):
        """POST an emoji-avatar request.

        Args:
            profile: The profile named in the path.
            payload: The JSON body.

        Returns:
            The test-client response.
        """
        return self.client.post(self._url(profile), payload, content_type="application/json", **_bearer(self.raw_key))

    def test_generates_and_stores_an_svg(self) -> None:
        """A recognized animal and palette colour produce a stored avatar."""
        response = self._post(self.profile, {"animal": "fox", "color": MaterialColor.GREEN.value})

        self.assertEqual(response.status_code, 200)
        self.profile.refresh_from_db()
        self.assertTrue(self.profile.avatar.name.endswith(".svg"))

    def test_lowercase_hex_is_accepted(self) -> None:
        """Client-side pickers routinely lowercase hex; that must still work."""
        response = self._post(self.profile, {"animal": "owl", "color": MaterialColor.BLUE.value.lower()})
        self.assertEqual(response.status_code, 200)

    def test_unknown_animal_is_400(self) -> None:
        """Unlike the site's picker, the API tells a client it sent a typo."""
        response = self._post(self.profile, {"animal": "wyvern", "color": MaterialColor.GREEN.value})
        self.assertEqual(response.status_code, 400)

    def test_colour_outside_the_palette_is_400(self) -> None:
        """The colour is interpolated into generated SVG, so the set stays closed."""
        response = self._post(self.profile, {"animal": "fox", "color": '"/><script>alert(1)</script>'})

        self.assertEqual(response.status_code, 400)
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.avatar)

    def test_another_profile_is_404(self) -> None:
        """Only your own avatar, and no hint that the slug resolves."""
        response = self._post(self.other, {"animal": "fox", "color": MaterialColor.GREEN.value})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"error": "No such profile."})


class ProfileAnnotationTests(_SocialProfileTestCase):
    """GET annotations; PUT/DELETE nickname and trust."""

    def _annotations_url(self, profile: Profile) -> str:
        """The annotations route for a profile.

        Args:
            profile: The subject named in the path.

        Returns:
            The reversed URL.
        """
        return reverse("external_api:profiles.annotations", kwargs={"profile_slug": self._slug(profile)})

    def _nickname_url(self, profile: Profile) -> str:
        """The nickname route for a profile.

        Args:
            profile: The subject named in the path.

        Returns:
            The reversed URL.
        """
        return reverse("external_api:profiles.nickname", kwargs={"profile_slug": self._slug(profile)})

    def _trust_url(self, profile: Profile) -> str:
        """The trust route for a profile.

        Args:
            profile: The subject named in the path.

        Returns:
            The reversed URL.
        """
        return reverse("external_api:profiles.trust", kwargs={"profile_slug": self._slug(profile)})

    def test_empty_annotations_read_as_nulls(self) -> None:
        """An un-annotated subject is nulls and a zero, not a 404."""
        response = self.client.get(self._annotations_url(self.other), **_bearer(self.raw_key))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"nickname": None, "trust": None, "note_count": 0})

    def test_nickname_round_trips(self) -> None:
        """PUT stores it, and the annotations payload reflects it immediately."""
        response = self.client.put(
            self._nickname_url(self.other),
            {"nickname": "Ladder Guy"},
            content_type="application/json",
            **_bearer(self.raw_key),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["nickname"], "Ladder Guy")
        self.assertEqual(ProfileNickname.objects.for_pair(self.profile, self.other).count(), 1)

    def test_nickname_put_replaces_rather_than_duplicating(self) -> None:
        """The row is a singleton per pair, so PUT is idempotent."""
        for value in ("First", "Second"):
            self.client.put(self._nickname_url(self.other), {"nickname": value}, content_type="application/json", **_bearer(self.raw_key))

        rows = ProfileNickname.objects.for_pair(self.profile, self.other)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().nickname, "Second")

    def test_nickname_over_the_length_limit_is_400(self) -> None:
        """The column is 100 characters; a longer value is refused, not truncated."""
        response = self.client.put(
            self._nickname_url(self.other),
            {"nickname": "x" * 101},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 400)

    def test_nickname_delete_clears_it_and_is_idempotent(self) -> None:
        """A second DELETE still succeeds - safe for a retried mobile request."""
        ProfileNickname.objects.create(author=self.profile, subject=self.other, nickname="Ladder Guy")

        first = self.client.delete(self._nickname_url(self.other), **_bearer(self.raw_key))
        second = self.client.delete(self._nickname_url(self.other), **_bearer(self.raw_key))

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertIsNone(first.json()["nickname"])
        self.assertFalse(ProfileNickname.objects.for_pair(self.profile, self.other).exists())

    def test_trust_round_trips(self) -> None:
        """A rating in range is stored and echoed."""
        response = self.client.put(self._trust_url(self.other), {"rating": 4}, content_type="application/json", **_bearer(self.raw_key))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["trust"], 4)
        self.assertEqual(ProfileTrust.objects.for_pair(self.profile, self.other).first().rating, 4)

    def test_trust_outside_one_to_five_is_400_and_stores_nothing(self) -> None:
        """``update_or_create`` skips field validators, so the bound is enforced here."""
        for rating in (0, 6, -1):
            with self.subTest(rating=rating):
                response = self.client.put(self._trust_url(self.other), {"rating": rating}, content_type="application/json", **_bearer(self.raw_key))
                self.assertEqual(response.status_code, 400)
        self.assertFalse(ProfileTrust.objects.for_pair(self.profile, self.other).exists())

    def test_trust_delete_clears_it(self) -> None:
        """DELETE removes the singleton and reports the cleared state."""
        ProfileTrust.objects.create(author=self.profile, subject=self.other, rating=5)

        response = self.client.delete(self._trust_url(self.other), **_bearer(self.raw_key))

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["trust"])
        self.assertFalse(ProfileTrust.objects.for_pair(self.profile, self.other).exists())

    def test_note_count_counts_only_the_callers_own_notes(self) -> None:
        """Notes are per-author; another author's notes never inflate the count."""
        ProfileNote.objects.create(author=self.profile, subject=self.other, content="Met at the mill")
        ProfileNote.objects.create(author=self.profile, subject=self.other, content="Has a key")
        third_party = Profile.objects.get(user=baker.make(User))
        ProfileNote.objects.create(author=third_party, subject=self.other, content="Not mine")

        response = self.client.get(self._annotations_url(self.other), **_bearer(self.raw_key))

        self.assertEqual(response.json()["note_count"], 2)

    def test_self_annotation_is_400(self) -> None:
        """You cannot nickname or rate yourself."""
        nickname = self.client.put(self._nickname_url(self.profile), {"nickname": "Me"}, content_type="application/json", **_bearer(self.raw_key))
        trust = self.client.put(self._trust_url(self.profile), {"rating": 5}, content_type="application/json", **_bearer(self.raw_key))

        self.assertEqual(nickname.status_code, 400)
        self.assertEqual(trust.status_code, 400)

    def test_invisible_profile_is_404_on_every_route(self) -> None:
        """A subject whose visibility excludes the caller must not resolve at all."""
        self.other.profile_visibility = VisibilityChoice.FRIENDS
        self.other.save()

        responses = (
            self.client.get(self._annotations_url(self.other), **_bearer(self.raw_key)),
            self.client.put(self._nickname_url(self.other), {"nickname": "x"}, content_type="application/json", **_bearer(self.raw_key)),
            self.client.delete(self._nickname_url(self.other), **_bearer(self.raw_key)),
            self.client.put(self._trust_url(self.other), {"rating": 3}, content_type="application/json", **_bearer(self.raw_key)),
            self.client.delete(self._trust_url(self.other), **_bearer(self.raw_key)),
        )

        for response in responses:
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json(), {"error": "No such profile."})

    def test_unknown_slug_is_404(self) -> None:
        """An unknown slug answers exactly as an invisible one does."""
        response = self.client.get(
            reverse("external_api:profiles.annotations", kwargs={"profile_slug": "no-such-person"}),
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"error": "No such profile."})


class AnnotationsArePrivateToTheirAuthorTests(_SocialProfileTestCase):
    """The subject of an annotation must never be able to read it.

    This is the failure a queryset filtered on ``subject`` alone would cause:
    every author-side test above would still pass while the annotated person
    could read everyone's private opinion of them in one request.
    """

    def setUp(self) -> None:
        """Have the caller annotate the second account."""
        super().setUp()
        ProfileNickname.objects.create(author=self.profile, subject=self.other, nickname="Ladder Guy")
        ProfileTrust.objects.create(author=self.profile, subject=self.other, rating=2)
        ProfileNote.objects.create(author=self.profile, subject=self.other, content="Bailed on the last trip")

    def test_subject_reading_their_own_annotations_sees_nothing(self) -> None:
        """Asking about yourself returns *your* rows, which are empty - not theirs."""
        response = self.client.get(
            reverse("external_api:profiles.annotations", kwargs={"profile_slug": self._slug(self.other)}),
            **_bearer(self.other_key),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"nickname": None, "trust": None, "note_count": 0})

    def test_subject_reading_the_authors_profile_sees_nothing(self) -> None:
        """Nor is the row readable by looking the *author* up instead."""
        response = self.client.get(
            reverse("external_api:profiles.annotations", kwargs={"profile_slug": self._slug(self.profile)}),
            **_bearer(self.other_key),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"nickname": None, "trust": None, "note_count": 0})

    def test_subject_deleting_does_not_touch_the_authors_row(self) -> None:
        """A DELETE from the subject clears only their own (absent) annotations."""
        self.client.delete(
            reverse("external_api:profiles.nickname", kwargs={"profile_slug": self._slug(self.profile)}),
            **_bearer(self.other_key),
        )
        self.client.delete(
            reverse("external_api:profiles.trust", kwargs={"profile_slug": self._slug(self.profile)}),
            **_bearer(self.other_key),
        )

        self.assertTrue(ProfileNickname.objects.for_pair(self.profile, self.other).exists())
        self.assertTrue(ProfileTrust.objects.for_pair(self.profile, self.other).exists())

    def test_third_party_sees_nothing_either(self) -> None:
        """Privacy is per-author, not merely "not the subject"."""
        stranger_user = baker.make(User, username="stranger")
        stranger_key = _key_with_scopes(stranger_user, ApiKeyScope.SOCIAL_READ)

        response = self.client.get(
            reverse("external_api:profiles.annotations", kwargs={"profile_slug": self._slug(self.other)}),
            **_bearer(stranger_key),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"nickname": None, "trust": None, "note_count": 0})


class AnnotationScopeEnforcementTests(_SocialProfileTestCase):
    """The annotation routes fail closed without their scope."""

    def test_reading_requires_social_read(self) -> None:
        """A key with no social scope reaches none of it."""
        raw_key = _key_with_scopes(self.user, ApiKeyScope.PROFILE_READ)
        response = self.client.get(
            reverse("external_api:profiles.annotations", kwargs={"profile_slug": self._slug(self.other)}),
            **_bearer(raw_key),
        )
        self.assertEqual(response.status_code, 403)

    def test_writing_requires_social_write(self) -> None:
        """``social:read`` must not be able to annotate."""
        raw_key = _key_with_scopes(self.user, ApiKeyScope.SOCIAL_READ)
        slug = self._slug(self.other)

        nickname = self.client.put(
            reverse("external_api:profiles.nickname", kwargs={"profile_slug": slug}),
            {"nickname": "x"},
            content_type="application/json",
            **_bearer(raw_key),
        )
        trust = self.client.delete(reverse("external_api:profiles.trust", kwargs={"profile_slug": slug}), **_bearer(raw_key))

        self.assertEqual(nickname.status_code, 403)
        self.assertEqual(trust.status_code, 403)
