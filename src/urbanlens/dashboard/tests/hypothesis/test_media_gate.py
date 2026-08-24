"""Tests for the authenticated media gate (dashboard.controllers.media.MediaGateView).

Covers:
- Anonymous requests are denied (redirected to login).
- The uploading owner can always fetch their own image bytes.
- An unrelated authenticated user is denied another user's photo (404).
- A friend passing the photo-visibility rules can fetch it.
- Direct-message attachments are participant-only.
- Path traversal outside MEDIA_ROOT is a 404, as is a missing file.
- Avatars and orphan files are authenticated-only.
- Production mode (MEDIA_X_ACCEL=True) answers with an X-Accel-Redirect header
  and no body instead of streaming the file.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import tempfile

from django.contrib.auth.models import User
from django.test import override_settings
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType, Permission
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice

_IMAGE_BYTES = b"fake-image-bytes-for-media-gate"


def _new_user() -> User:
    """A fresh User (its Profile is auto-created by the post_save signal)."""
    return baker.make(User)


class MediaGateTests(TestCase):
    """End-to-end tests for /media/<path> through the URLconf."""

    def setUp(self):
        """Point MEDIA_ROOT at a throwaway temp dir and seed one owned image file."""
        self._media_root = tempfile.mkdtemp(prefix="ul_media_gate_")
        self.addCleanup(shutil.rmtree, self._media_root, ignore_errors=True)
        self._overrides = override_settings(MEDIA_ROOT=self._media_root, MEDIA_X_ACCEL=False)
        self._overrides.enable()
        self.addCleanup(self._overrides.disable)

        (Path(self._media_root) / "pin_images").mkdir(parents=True)
        (Path(self._media_root) / "avatars").mkdir(parents=True)
        self._write_media("pin_images/owned.png")

        self.owner_user = _new_user()
        self.owner: Profile = self.owner_user.profile
        self.image = baker.make(Image, image="pin_images/owned.png", profile=self.owner)

    def _write_media(self, rel_path: str, data: bytes = _IMAGE_BYTES) -> None:
        """Write a fake media file under the temp MEDIA_ROOT."""
        target = Path(self._media_root) / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def _get_bytes(self, response) -> bytes:
        """Materialize a (possibly streaming) response body.

        Closes only the underlying file handle (so the temp MEDIA_ROOT can be
        removed on Windows) - never ``response.close()``, which fires the
        ``request_finished`` signal and would close the test DB connection.
        """
        if getattr(response, "streaming", False):
            data = b"".join(response.streaming_content)
            file_to_stream = getattr(response, "file_to_stream", None)
            if file_to_stream is not None:
                file_to_stream.close()
            return data
        return response.content

    # -- Authentication ---------------------------------------------------------

    def test_anonymous_request_is_denied(self):
        response = self.client.get("/media/pin_images/owned.png")
        self.assertIn(response.status_code, (301, 302, 401, 403), "anonymous media request must not receive file content")
        if response.status_code in (301, 302):
            self.assertIn("login", response.headers.get("Location", ""), "anonymous request should bounce to the login page")

    # -- Ownership / visibility -------------------------------------------------

    def test_owner_fetches_own_image(self):
        self.client.force_login(self.owner_user)
        response = self.client.get("/media/pin_images/owned.png")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._get_bytes(response), _IMAGE_BYTES)

    def test_unrelated_user_is_denied(self):
        # Default photo_upload_visibility is ANYTHING_IN_COMMON; a stranger with
        # no friendship/pin/trip overlap fails it and must get an opaque 404.
        stranger = _new_user()
        self.client.force_login(stranger)
        response = self.client.get("/media/pin_images/owned.png")
        self.assertEqual(response.status_code, 404)

    def _befriend(self):
        """An accepted friendship from the owner to a new user."""
        friend_user = _new_user()
        Friendship.objects.create(
            from_profile=self.owner,
            to_profile=friend_user.profile,
            status=FriendshipStatus.ACCEPTED,
            relationship_type=FriendshipType.FRIEND,
            permissions=Permission.VIEW_PROFILE,
        )
        return friend_user

    def test_a_friend_cannot_fetch_a_photo_that_was_never_shared(self):
        """A photo is private until its owner shares it, and being someone's
        friend is not the same as being shown their photo.

        Visibility is two gates: the photo has to have been shared to a wiki, and
        then the uploader's setting decides which of the people who can reach
        that wiki may see it. This test used to assert the friendship alone was
        enough, which was the first gate missing entirely.
        """
        self.client.force_login(self._befriend())

        response = self.client.get("/media/pin_images/owned.png")

        self.assertEqual(response.status_code, 404)

    def test_a_friend_can_fetch_a_photo_that_was_shared(self):
        """And with both gates open, they can - which is the point of sharing.

        Both gates means both. The friendship opens the uploader's setting; the
        friend's own pin at the place is what brings the wiki within reach. This
        fixture used to make a wiki nobody had pinned and still expect a 200,
        which only passed while the container gate ignored *which* wiki.
        """
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.pin.model import Pin
        from urbanlens.dashboard.models.wiki.model import Wiki

        location = baker.make(Location, latitude=41.7361, longitude=-73.9361)
        wiki = baker.make(Wiki, location=location)
        Image.objects.filter(pk=self.image.pk).update(wiki=wiki)
        friend = self._befriend()
        baker.make(Pin, profile=friend.profile, location=location, parent_pin=None)
        self.client.force_login(friend)

        response = self.client.get("/media/pin_images/owned.png")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._get_bytes(response), _IMAGE_BYTES)

    def test_a_friend_cannot_fetch_a_photo_on_a_wiki_they_cannot_reach(self):
        """Wiki access is earned per place, and a friendship does not carry across.

        The uploader's setting admits this friend; the wiki is at a place they
        have never pinned, so the container gate is shut and the setting never
        gets to speak.
        """
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.wiki.model import Wiki

        wiki = baker.make(Wiki, location=baker.make(Location, latitude=47.6062, longitude=-122.3321))
        Image.objects.filter(pk=self.image.pk).update(wiki=wiki)
        self.client.force_login(self._befriend())

        response = self.client.get("/media/pin_images/owned.png")

        self.assertEqual(response.status_code, 404)

    def test_dm_attachment_is_participant_only(self):
        from urbanlens.dashboard.models.direct_messages.model import DirectMessage

        recipient_user = _new_user()
        outsider_user = _new_user()
        message = baker.make(DirectMessage, sender=self.owner, recipient=recipient_user.profile)
        self._write_media("pin_images/dm.png")
        baker.make(Image, image="pin_images/dm.png", profile=self.owner, direct_message=message)

        self.client.force_login(recipient_user)
        response = self.client.get("/media/pin_images/dm.png")
        self.assertEqual(response.status_code, 200, "the DM recipient must be able to fetch the attachment")
        self._get_bytes(response)

        self.client.force_login(outsider_user)
        response = self.client.get("/media/pin_images/dm.png")
        self.assertEqual(response.status_code, 404, "a non-participant must not fetch a DM attachment")

    def test_avatar_is_fetchable_by_any_authenticated_user(self):
        self._write_media("avatars/someone.png")
        Profile.objects.filter(pk=self.owner.pk).update(avatar="avatars/someone.png")
        viewer = _new_user()
        self.client.force_login(viewer)
        response = self.client.get("/media/avatars/someone.png")
        self.assertEqual(response.status_code, 200)
        self._get_bytes(response)

    def test_orphan_file_is_authenticated_only(self):
        self._write_media("pin_images/orphan.png")
        viewer = _new_user()
        self.client.force_login(viewer)
        response = self.client.get("/media/pin_images/orphan.png")
        self.assertEqual(response.status_code, 200, "a file with no owning row falls back to authenticated-only access")
        self._get_bytes(response)

    # -- Path safety ------------------------------------------------------------

    def test_path_traversal_is_404(self):
        # A real, sensitive file OUTSIDE MEDIA_ROOT that ../ would reach.
        secret = Path(self._media_root).parent / "ul_media_gate_secret.txt"
        secret.write_bytes(b"secret-settings")
        self.addCleanup(secret.unlink)

        self.client.force_login(self.owner_user)
        response = self.client.get("/media/../ul_media_gate_secret.txt")
        self.assertEqual(response.status_code, 404)

    def test_nested_traversal_is_404(self):
        self.client.force_login(self.owner_user)
        response = self.client.get("/media/pin_images/../../../etc/passwd")
        self.assertEqual(response.status_code, 404)

    def test_missing_file_is_404(self):
        self.client.force_login(self.owner_user)
        response = self.client.get("/media/pin_images/does-not-exist.png")
        self.assertEqual(response.status_code, 404)

    # -- Production (nginx X-Accel-Redirect) mode -------------------------------

    def test_production_mode_returns_x_accel_redirect(self):
        self.client.force_login(self.owner_user)
        with override_settings(MEDIA_X_ACCEL=True):
            response = self.client.get("/media/pin_images/owned.png")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("X-Accel-Redirect"), "/_protected_media/pin_images/owned.png")
        self.assertEqual(response.content, b"", "in X-Accel mode nginx streams the body, Django must not")
        self.assertNotIn("Content-Type", response.headers, "Content-Type is left for nginx to derive")

    def test_production_mode_still_denies_stranger(self):
        stranger = _new_user()
        self.client.force_login(stranger)
        with override_settings(MEDIA_X_ACCEL=True):
            response = self.client.get("/media/pin_images/owned.png")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("X-Accel-Redirect", response.headers)


class CommentImageMediaGateTests(TestCase):
    """``comment_images/`` must also respect the comment author's ``comment_visibility``.

    Host-level access (owning the pin, being able to see the wiki, being a
    trip member) is necessary but not sufficient - the same author privacy
    setting that hides a comment's text from ``visible_comment_tree``/
    ``build_comment_tree`` must hide its attached image too, and must keep
    hiding it after the author tightens the setting even if a viewer already
    has the file's URL.
    """

    def setUp(self):
        from urbanlens.dashboard.models.pin.model import Pin
        from urbanlens.dashboard.models.wiki.model import Wiki

        self._media_root = tempfile.mkdtemp(prefix="ul_media_gate_comments_")
        self.addCleanup(shutil.rmtree, self._media_root, ignore_errors=True)
        self._overrides = override_settings(MEDIA_ROOT=self._media_root, MEDIA_X_ACCEL=False)
        self._overrides.enable()
        self.addCleanup(self._overrides.disable)
        (Path(self._media_root) / "comment_images").mkdir(parents=True)

        self.owner_user = _new_user()
        self.owner: Profile = self.owner_user.profile
        self.author_user = _new_user()
        self.author: Profile = self.author_user.profile

        self.pin = baker.make(Pin, profile=self.owner)
        self.wiki = baker.make(Wiki, location=self.pin.location)

    def _set_comment_visibility(self, visibility: str) -> None:
        self.author.comment_visibility = visibility
        self.author.save(update_fields=["comment_visibility"])

    def test_wiki_comment_image_hidden_when_author_restricts_to_no_one(self):
        from urbanlens.dashboard.models.comments.model import Comment

        self._set_comment_visibility(VisibilityChoice.NO_ONE)
        self._write_media("comment_images/wiki.png")
        baker.make(Comment, pin=None, wiki=self.wiki, profile=self.author, image="comment_images/wiki.png")

        self.client.force_login(self.owner_user)
        response = self.client.get("/media/comment_images/wiki.png")
        self.assertEqual(response.status_code, 404, "a viewer who could see the wiki must still be denied once the author restricts comment_visibility")

    def test_wiki_comment_image_visible_when_author_allows_anyone(self):
        from urbanlens.dashboard.models.comments.model import Comment

        self._set_comment_visibility(VisibilityChoice.ANYONE)
        self._write_media("comment_images/wiki.png")
        baker.make(Comment, pin=None, wiki=self.wiki, profile=self.author, image="comment_images/wiki.png")

        self.client.force_login(self.owner_user)
        response = self.client.get("/media/comment_images/wiki.png")
        self.assertEqual(response.status_code, 200)
        self._get_bytes(response)

    def test_pin_comment_image_hidden_from_owner_when_author_restricts_to_no_one(self):
        from urbanlens.dashboard.models.comments.model import Comment

        self._set_comment_visibility(VisibilityChoice.NO_ONE)
        self._write_media("comment_images/pin.png")
        baker.make(Comment, pin=self.pin, wiki=None, profile=self.author, image="comment_images/pin.png")

        self.client.force_login(self.owner_user)
        response = self.client.get("/media/comment_images/pin.png")
        self.assertEqual(response.status_code, 404, "the pin owner must still be denied once the comment author restricts comment_visibility")

    def test_pin_comment_image_visible_to_owner_when_author_allows_anyone(self):
        from urbanlens.dashboard.models.comments.model import Comment

        self._set_comment_visibility(VisibilityChoice.ANYONE)
        self._write_media("comment_images/pin.png")
        baker.make(Comment, pin=self.pin, wiki=None, profile=self.author, image="comment_images/pin.png")

        self.client.force_login(self.owner_user)
        response = self.client.get("/media/comment_images/pin.png")
        self.assertEqual(response.status_code, 200)
        self._get_bytes(response)

    def test_trip_comment_image_hidden_from_member_when_author_restricts_to_no_one(self):
        from urbanlens.dashboard.models.trips.model import Trip, TripComment, TripMembership

        self._set_comment_visibility(VisibilityChoice.NO_ONE)
        trip = baker.make(Trip, creator=self.author)
        TripMembership.objects.create(trip=trip, profile=self.owner)
        self._write_media("comment_images/trip.png")
        baker.make(TripComment, trip=trip, author=self.author, image="comment_images/trip.png")

        self.client.force_login(self.owner_user)
        response = self.client.get("/media/comment_images/trip.png")
        self.assertEqual(response.status_code, 404, "a fellow trip member must still be denied once the author restricts comment_visibility")

    def test_trip_comment_image_visible_to_member_when_author_allows_anyone(self):
        from urbanlens.dashboard.models.trips.model import Trip, TripComment, TripMembership

        self._set_comment_visibility(VisibilityChoice.ANYONE)
        trip = baker.make(Trip, creator=self.author)
        TripMembership.objects.create(trip=trip, profile=self.owner)
        self._write_media("comment_images/trip.png")
        baker.make(TripComment, trip=trip, author=self.author, image="comment_images/trip.png")

        self.client.force_login(self.owner_user)
        response = self.client.get("/media/comment_images/trip.png")
        self.assertEqual(response.status_code, 200)
        self._get_bytes(response)

    def _write_media(self, rel_path: str, data: bytes = _IMAGE_BYTES) -> None:
        """Write a fake media file under the temp MEDIA_ROOT."""
        target = Path(self._media_root) / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def _get_bytes(self, response) -> bytes:
        """Materialize a (possibly streaming) response body - see MediaGateTests."""
        if getattr(response, "streaming", False):
            data = b"".join(response.streaming_content)
            file_to_stream = getattr(response, "file_to_stream", None)
            if file_to_stream is not None:
                file_to_stream.close()
            return data
        return response.content
