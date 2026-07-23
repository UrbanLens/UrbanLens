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
from urbanlens.dashboard.models.profile.model import Profile

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

    def test_friend_passing_visibility_can_fetch(self):
        friend_user = _new_user()
        Friendship.objects.create(
            from_profile=self.owner,
            to_profile=friend_user.profile,
            status=FriendshipStatus.ACCEPTED,
            relationship_type=FriendshipType.FRIEND,
            permissions=Permission.VIEW_PROFILE,
        )
        self.client.force_login(friend_user)
        response = self.client.get("/media/pin_images/owned.png")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._get_bytes(response), _IMAGE_BYTES)

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
