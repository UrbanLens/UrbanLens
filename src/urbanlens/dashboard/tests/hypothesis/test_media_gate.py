"""Tests for the authenticated media gate (dashboard.controllers.media.MediaGateView).

Covers:
- Anonymous requests are denied (redirected to login).
- The uploading owner can always fetch their own image bytes.
- An unrelated authenticated user is denied another user's photo (404).
- A friend passing the photo-visibility rules can fetch it.
- Direct-message attachments are participant-only.
- Path traversal outside MEDIA_ROOT is a 404, as is a missing file.
- Thumbnails follow the same rules as the photo they preview.
- Avatars are authenticated-only; files with no owning row, and directories
  with no registered authorizer, are denied.
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
from urbanlens.dashboard.models.images.model import Image, QuotaExemption
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

    def test_response_allows_same_origin_framing(self):
        """The Vault document lightbox previews a document in an <iframe> on this
        same site; the project-wide X-Frame-Options: DENY default (settings.base)
        would block even that same-origin case, so this view must override it to
        SAMEORIGIN rather than inherit the default.
        """
        self.client.force_login(self.owner_user)
        response = self.client.get("/media/pin_images/owned.png")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("X-Frame-Options"), "SAMEORIGIN")

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

    def test_file_with_no_owning_row_is_denied(self):
        """A file the gate cannot attribute is refused, not served.

        An orphan left behind by a deleted row is indistinguishable from a live
        file whose owning row this viewer is not allowed to learn about, and
        assuming the harmless case is what served every thumbnail to every
        account. Nobody holds a URL for a real orphan anyway.
        """
        self._write_media("pin_images/orphan.png")
        viewer = _new_user()
        self.client.force_login(viewer)

        response = self.client.get("/media/pin_images/orphan.png")

        self.assertEqual(response.status_code, 404)

    def test_unregistered_path_family_is_denied(self):
        """A directory with no registered authorizer is refused by default.

        The point of the registry is that forgetting to add one fails closed.
        """
        self._write_media("unregistered_family/leak.png")
        viewer = _new_user()
        self.client.force_login(viewer)

        response = self.client.get("/media/unregistered_family/leak.png")

        self.assertEqual(response.status_code, 404)

    # -- Thumbnails -------------------------------------------------------------
    #
    # A thumbnail is a second stored file in a second column. Authorizing only
    # the `image` column meant no thumbnail ever resolved to an owner, so every
    # one of them took the permissive no-owner branch and was readable by any
    # logged-in account - visibility settings, share revocation and the DM
    # participant rule all unreachable for the 400px copy of the photo.

    def test_owner_fetches_own_thumbnail(self):
        self._write_media("pin_images/thumbs/owned.webp")
        Image.objects.filter(pk=self.image.pk).update(thumbnail="pin_images/thumbs/owned.webp")
        self.client.force_login(self.owner_user)

        response = self.client.get("/media/pin_images/thumbs/owned.webp")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._get_bytes(response), _IMAGE_BYTES)

    def test_unrelated_user_is_denied_a_thumbnail(self):
        self._write_media("pin_images/thumbs/owned.webp")
        Image.objects.filter(pk=self.image.pk).update(thumbnail="pin_images/thumbs/owned.webp")
        stranger = _new_user()
        self.client.force_login(stranger)

        response = self.client.get("/media/pin_images/thumbs/owned.webp")

        self.assertEqual(response.status_code, 404, "a stranger denied the photo must be denied its preview too")

    def test_thumbnail_of_a_dm_attachment_is_participant_only(self):
        """The DM participant rule has to reach the preview, not just the file."""
        from urbanlens.dashboard.models.direct_messages.model import DirectMessage

        sender = _new_user()
        recipient = _new_user()
        dm = baker.make(DirectMessage, sender=sender.profile, recipient=recipient.profile)
        self._write_media("pin_images/dm-thumb.webp")
        baker.make(
            Image,
            image="pin_images/dm-orig.png",
            thumbnail="pin_images/dm-thumb.webp",
            profile=sender.profile,
            direct_message=dm,
        )

        self.client.force_login(recipient)
        allowed = self.client.get("/media/pin_images/dm-thumb.webp")
        self.assertEqual(allowed.status_code, 200, "the recipient of the message may see its attachment preview")
        self._get_bytes(allowed)

        self.client.force_login(_new_user())
        denied = self.client.get("/media/pin_images/dm-thumb.webp")
        self.assertEqual(denied.status_code, 404, "a non-participant must not fetch a DM attachment's preview")

    # -- Marker thumbnails --------------------------------------------------------
    #
    # A third stored file in a third column, same failure mode as the grid
    # thumbnail above if `authorize_image` ever stops searching it.

    def test_owner_fetches_own_marker_thumbnail(self):
        self._write_media("pin_images/markers/owned-marker.webp")
        Image.objects.filter(pk=self.image.pk).update(marker_thumbnail="pin_images/markers/owned-marker.webp")
        self.client.force_login(self.owner_user)

        response = self.client.get("/media/pin_images/markers/owned-marker.webp")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._get_bytes(response), _IMAGE_BYTES)

    def test_unrelated_user_is_denied_a_marker_thumbnail(self):
        self._write_media("pin_images/markers/owned-marker.webp")
        Image.objects.filter(pk=self.image.pk).update(marker_thumbnail="pin_images/markers/owned-marker.webp")
        stranger = _new_user()
        self.client.force_login(stranger)

        response = self.client.get("/media/pin_images/markers/owned-marker.webp")

        self.assertEqual(response.status_code, 404, "a stranger denied the photo must be denied its marker preview too")

    # -- Files backed by more than one row ---------------------------------------

    def test_a_share_recipient_reaches_a_photo_they_were_given(self):
        """Accepting a share hands over a row, not a second copy of the bytes.

        Several rows therefore point at one storage key, and whichever one the
        lookup happened to return decided the answer - so a recipient could be
        refused a photo that was deliberately shared with them.
        """
        recipient_user = _new_user()
        baker.make(
            Image,
            image="pin_images/owned.png",
            profile=recipient_user.profile,
            quota_exempt_reason=QuotaExemption.SHARED_COPY,
        )
        self.client.force_login(recipient_user)

        response = self.client.get("/media/pin_images/owned.png")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._get_bytes(response), _IMAGE_BYTES)

    def test_a_stranger_is_still_denied_a_file_two_other_people_share(self):
        """The extra row must not widen who the file reaches."""
        other_user = _new_user()
        baker.make(Image, image="pin_images/owned.png", profile=other_user.profile, quota_exempt_reason=QuotaExemption.SHARED_COPY)
        self.client.force_login(_new_user())

        response = self.client.get("/media/pin_images/owned.png")

        self.assertEqual(response.status_code, 404)

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


class SafetyCheckinMediaGateTests(TestCase):
    """A check-in is a container, and the people watching it can see its photos.

    The container gate reads reachability off the photo's container. A check-in
    photo has no wiki, so a gate that asked only about wikis denied its bytes to
    everybody but the uploader - while the gallery panel happily listed them to
    an accepted partner, who saw a grid of broken images on the page that exists
    to tell them somebody is overdue.
    """

    def setUp(self):
        self._media_root = tempfile.mkdtemp(prefix="ul_media_gate_safety_")
        self.addCleanup(shutil.rmtree, self._media_root, ignore_errors=True)
        self._overrides = override_settings(MEDIA_ROOT=self._media_root, MEDIA_X_ACCEL=False)
        self._overrides.enable()
        self.addCleanup(self._overrides.disable)
        (Path(self._media_root) / "pin_images").mkdir(parents=True)
        (Path(self._media_root) / "pin_images" / "checkin.png").write_bytes(_IMAGE_BYTES)

        self.owner_user = _new_user()
        self.owner: Profile = self.owner_user.profile
        self.checkin = self._make_checkin()
        self.image = baker.make(Image, image="pin_images/checkin.png", profile=self.owner, safety_checkin=self.checkin)

    def _make_checkin(self):
        import datetime

        from django.utils import timezone

        return baker.make(
            "dashboard.SafetyCheckin",
            profile=self.owner,
            title="Test hike",
            checkin_by=timezone.now() + datetime.timedelta(hours=2),
            grace_period=datetime.timedelta(hours=1),
        )

    def _fetch(self) -> int:
        return self.client.get("/media/pin_images/checkin.png").status_code

    def test_an_accepted_partner_can_fetch_a_checkin_photo(self):
        from urbanlens.dashboard.models.safety.model import SafetyCheckinPartner, SafetyCheckinPartnerStatus

        partner_user = _new_user()
        SafetyCheckinPartner.objects.create(checkin=self.checkin, profile=partner_user.profile, invited_by=self.owner, status=SafetyCheckinPartnerStatus.ACCEPTED)
        self.client.force_login(partner_user)

        self.assertEqual(self._fetch(), 200, "an accepted safety partner could not load the check-in's photos")

    def test_an_invited_but_unaccepted_partner_cannot(self):
        """Being asked is not the same as having accepted - mirrors partnered_with."""
        from urbanlens.dashboard.models.safety.model import SafetyCheckinPartner, SafetyCheckinPartnerStatus

        invitee_user = _new_user()
        SafetyCheckinPartner.objects.create(checkin=self.checkin, profile=invitee_user.profile, invited_by=self.owner, status=SafetyCheckinPartnerStatus.INVITED)
        self.client.force_login(invitee_user)

        self.assertEqual(self._fetch(), 404)

    def test_a_stranger_cannot_fetch_a_checkin_photo(self):
        self.client.force_login(_new_user())

        self.assertEqual(self._fetch(), 404)

    def test_the_owner_can_fetch_their_own_checkin_photo(self):
        self.client.force_login(self.owner_user)

        self.assertEqual(self._fetch(), 200)


class SafetyContactTokenPhotoTests(TestCase):
    """A signed-out emergency contact can see the check-in's photos.

    An emergency contact is frequently somebody with no account at all - that is
    the point of the magic-link portal - so the login-gated media path can never
    serve them. Reaching the check-in is the whole barrier: a valid token is the
    credential, and it scopes to exactly one check-in's photos.
    """

    def setUp(self):
        import datetime

        from django.utils import timezone

        self._media_root = tempfile.mkdtemp(prefix="ul_media_gate_token_")
        self.addCleanup(shutil.rmtree, self._media_root, ignore_errors=True)
        self._overrides = override_settings(MEDIA_ROOT=self._media_root, MEDIA_X_ACCEL=False)
        self._overrides.enable()
        self.addCleanup(self._overrides.disable)
        (Path(self._media_root) / "pin_images").mkdir(parents=True)
        (Path(self._media_root) / "pin_images" / "tok.png").write_bytes(_IMAGE_BYTES)
        (Path(self._media_root) / "pin_images" / "other.png").write_bytes(_IMAGE_BYTES)

        self.owner: Profile = _new_user().profile
        self._now = timezone.now
        self._delta = datetime.timedelta

        self.checkin = self._checkin()
        self.image = baker.make(Image, image="pin_images/tok.png", profile=self.owner, safety_checkin=self.checkin)
        self.contact = baker.make("dashboard.SafetyCheckinContact", checkin=self.checkin, email="contact@example.com", contact_profile=None)

    def _checkin(self):
        return baker.make(
            "dashboard.SafetyCheckin",
            profile=self.owner,
            title="Test hike",
            checkin_by=self._now() + self._delta(hours=2),
            grace_period=self._delta(hours=1),
        )

    def _url(self, token, image_id) -> str:
        from django.urls import reverse

        return reverse("safety.contact.photo", args=[token, image_id])

    def test_a_signed_out_contact_can_fetch_the_photo(self):
        response = self.client.get(self._url(self.contact.token, self.image.pk))

        self.assertEqual(response.status_code, 200, "a valid magic-link token could not fetch the check-in's photo")
        self.assertEqual(b"".join(response.streaming_content) if response.streaming else response.content, _IMAGE_BYTES)

    def test_an_invalid_token_gets_nothing(self):
        import uuid

        response = self.client.get(self._url(uuid.uuid4(), self.image.pk))

        self.assertEqual(response.status_code, 404)

    def test_a_token_does_not_reach_another_checkins_photo(self):
        """The token is scoped to its own check-in, not to check-in photos at large."""
        other_image = baker.make(Image, image="pin_images/other.png", profile=self.owner, safety_checkin=self._checkin())

        response = self.client.get(self._url(self.contact.token, other_image.pk))

        self.assertEqual(response.status_code, 404, "a contact's token reached a photo on a different check-in")

    def test_the_portal_lists_the_photo(self):
        from django.urls import reverse

        response = self.client.get(reverse("safety.contact.portal", args=[self.contact.token]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self._url(self.contact.token, self.image.pk))
