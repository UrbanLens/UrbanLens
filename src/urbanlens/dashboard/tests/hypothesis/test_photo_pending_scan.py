"""``Image.pending_scan``: the window between an accepted upload and the task.

``prepare_photo_upload`` used to decode a photo with Pillow and byte-walk-strip
its metadata inside the request - both closed windows the sandbox tier (see
``services.sandbox.guard``) exists to keep out of that process. It now stores
the raw upload untouched and marks the row ``pending_scan``; the read/strip
work moves entirely into ``tasks.process_image_upload``, which already runs on
the sandbox queue. Three things have to hold for that trade to be safe rather
than a regression of the leak ``metadata_strip.py`` was written to close:

- a fresh photo upload never decodes anything in the request (verified via the
  sandbox guard's own enforcement, not by inspecting call sites);
- a pending row is invisible to everyone but its uploader, both for a direct
  media-URL fetch and in gallery listings;
- the task, not the request, is what clears it - on success, and (so a genuine
  processing failure doesn't hide a row forever) on giving up too.
"""

from __future__ import annotations

from fractions import Fraction
import io
import shutil
import tempfile
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase as DjangoTestCase, override_settings
from model_bakery import baker
from PIL import Image as PILImage

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.media.images import prepare_photo_upload
from urbanlens.dashboard.services.photos.uploads import upload_photo_for_owner

_ARTIST_TAG = 0x013B
_GPS_IFD = 0x8825


def _jpeg_with_gps() -> bytes:
    """A minimal JPEG carrying a GPS position and an Artist tag."""
    exif = PILImage.Exif()
    exif[_ARTIST_TAG] = "A Photographer"
    gps = exif.get_ifd(_GPS_IFD)
    gps[1], gps[2] = "N", (Fraction(42), Fraction(39), Fraction(9))
    gps[3], gps[4] = "W", (Fraction(73), Fraction(45), Fraction(22))
    buf = io.BytesIO()
    PILImage.new("RGB", (48, 36), color=(10, 20, 30)).save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


def _upload(name: str = "shot.jpg", data: bytes | None = None) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, data if data is not None else _jpeg_with_gps(), content_type="image/jpeg")


@override_settings(UL_PROCESS_ROLE="web", UL_UNTRUSTED_PARSE_POLICY="deny")
class PrepareUploadNeverDecodesTests(DjangoTestCase):
    """The request-time half: no Pillow call, under any process role."""

    def test_prepare_photo_upload_does_not_decode(self) -> None:
        # If this raised UnsandboxedParseError, prepare_photo_upload would be
        # calling a guarded extractor - the exact regression this module exists
        # to catch. UL_PROCESS_ROLE=web + policy=deny mean any @untrusted_parse
        # call anywhere in this stack raises.
        prepared = prepare_photo_upload(_upload(), profile=None)
        self.assertEqual(prepared.metadata, {"pending_scan": True})
        self.assertIsNone(prepared.metadata_caption)

    def test_the_stored_file_is_the_upload_untouched(self) -> None:
        raw = _jpeg_with_gps()
        prepared = prepare_photo_upload(_upload(data=raw), profile=None)
        prepared.file.seek(0)
        self.assertEqual(prepared.file.read(), raw)
        self.assertEqual(prepared.size, len(raw))


class PendingScanUploadFlowTests(TestCase):
    """``upload_photo_for_owner`` end to end: creation, visibility, and clearing."""

    def setUp(self) -> None:
        self.owner_user = baker.make(User)
        self.owner: Profile = self.owner_user.profile
        self.stranger_user = baker.make(User)
        self.stranger: Profile = self.stranger_user.profile
        self.location = baker.make(Location)
        self.pin = baker.make(Pin, profile=self.owner, location=self.location)

        self._media_root = tempfile.mkdtemp(prefix="ul_pending_scan_")
        self.addCleanup(shutil.rmtree, self._media_root, ignore_errors=True)
        overrides = override_settings(MEDIA_ROOT=self._media_root, MEDIA_X_ACCEL=False)
        overrides.enable()
        self.addCleanup(overrides.disable)

    def _upload(self, **kwargs) -> Image:
        with patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task"):
            result = upload_photo_for_owner(self.pin, self.owner, _upload(**kwargs))
        assert isinstance(result, Image), getattr(result, "message", result)
        return result

    def test_a_fresh_upload_is_pending(self) -> None:
        image = self._upload()
        self.assertTrue(image.pending_scan)

    def test_exif_is_not_populated_until_the_task_runs(self) -> None:
        # Before this change these came back populated synchronously - that
        # synchronous read is exactly the decode this redesign removes.
        image = self._upload()
        self.assertIsNone(image.exif_data)
        self.assertIsNone(image.author)
        self.assertIsNone(image.latitude)

    def test_owner_can_fetch_their_own_pending_image(self) -> None:
        image = self._upload()
        self.client.force_login(self.owner_user)
        response = self.client.get(f"/media/{image.image.name}")
        self.assertEqual(response.status_code, 200)

    def test_stranger_cannot_fetch_a_pending_image(self) -> None:
        image = self._upload()
        self.client.force_login(self.stranger_user)
        response = self.client.get(f"/media/{image.image.name}")
        self.assertEqual(response.status_code, 404, "a pending row must be indistinguishable from a missing file")

    def test_pending_image_is_excluded_from_a_strangers_gallery_listing(self) -> None:
        image = self._upload()
        self.assertNotIn(image, list(Image.objects.filter(pk=image.pk).visible_to(self.stranger)))

    def test_pending_image_is_included_in_the_owners_own_listing(self) -> None:
        image = self._upload()
        self.assertIn(image, list(Image.objects.filter(pk=image.pk).visible_to(self.owner)))

    def test_processing_clears_pending_scan_and_populates_metadata(self) -> None:
        from urbanlens.dashboard.tasks import process_image_upload

        image = self._upload()
        process_image_upload(image.pk)
        image.refresh_from_db()
        self.assertFalse(image.pending_scan)
        self.assertEqual(image.author, "A Photographer")
        self.assertIsNotNone(image.latitude)

    def test_once_cleared_a_viewer_with_the_right_visibility_can_fetch_it(self) -> None:
        # visible_to() gates a pin-only photo to its uploader alone, full stop -
        # see its docstring - so demonstrating "pending_scan was the remaining
        # blocker" needs a photo actually reachable by someone else once cleared:
        # on a wiki, viewed by a friend who has pinned that wiki's location. Same
        # combination test_media_gate.py's test_a_friend_can_fetch_a_photo_that_was_shared
        # uses to open both of visible_to()'s gates.
        from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType, Permission
        from urbanlens.dashboard.models.friendship.model import Friendship
        from urbanlens.dashboard.models.wiki.model import Wiki
        from urbanlens.dashboard.tasks import process_image_upload

        wiki = baker.make(Wiki, location=self.location)
        image = self._upload()
        Image.objects.filter(pk=image.pk).update(wiki=wiki)
        image.refresh_from_db()

        Friendship.objects.create(from_profile=self.owner, to_profile=self.stranger, status=FriendshipStatus.ACCEPTED, relationship_type=FriendshipType.FRIEND, permissions=Permission.VIEW_PROFILE)
        baker.make(Pin, profile=self.stranger, location=self.location)

        self.client.force_login(self.stranger_user)
        self.assertEqual(self.client.get(f"/media/{image.image.name}").status_code, 404, "still pending - must be denied even with both other gates open")

        process_image_upload(image.pk)
        image.refresh_from_db()
        response = self.client.get(f"/media/{image.image.name}")
        self.assertEqual(response.status_code, 200)

    def test_a_transient_open_failure_retries_before_giving_up(self) -> None:
        # _process_photo_upload swallows OSError/ValueError internally and
        # returns None rather than raising, so process_image_upload's own
        # autoretry_for=(OSError,) never sees this failure - the retry has to
        # be explicit. .apply() (not calling the task function directly) is
        # what actually drives Celery's retry loop; countdown values are not
        # really slept during eager/direct execution.
        from urbanlens.dashboard.tasks import process_image_upload

        image = self._upload()
        calls = []

        def _fail_then_never_succeed(*args, **kwargs):
            calls.append(1)

        with patch("urbanlens.dashboard.tasks._process_photo_upload", side_effect=_fail_then_never_succeed):
            result = process_image_upload.apply(args=(image.pk,))

        self.assertEqual(len(calls), 4, "one initial attempt plus 3 retries (max_retries=3)")
        self.assertFalse(result.get())

    def test_a_transient_open_failure_that_clears_on_retry_leaves_the_row_intact_and_pending(self) -> None:
        # The realistic case a retry exists for: the second attempt succeeds.
        # The row must survive (not be rejected) and end up fully processed.
        from urbanlens.dashboard.tasks import _process_photo_upload as real_process_photo_upload, process_image_upload

        image = self._upload()
        attempts = []

        def _fail_once_then_succeed(image_arg, image_id_arg, strip_location_arg):
            attempts.append(1)
            if len(attempts) == 1:
                return None
            return real_process_photo_upload(image_arg, image_id_arg, strip_location_arg)

        with patch("urbanlens.dashboard.tasks._process_photo_upload", side_effect=_fail_once_then_succeed):
            result = process_image_upload.apply(args=(image.pk,))

        self.assertEqual(len(attempts), 2)
        self.assertTrue(result.get())
        self.assertTrue(Image.objects.filter(pk=image.pk).exists(), "the row must not be rejected once a retry succeeds")
        image.refresh_from_db()
        self.assertFalse(image.pending_scan)
        self.assertEqual(image.author, "A Photographer")

    def test_permanent_failure_on_a_fresh_upload_rejects_it_rather_than_publishing_it_raw(self) -> None:
        # Once retries are exhausted, a still-pending row has no "already
        # safe, just unprocessed" fallback available (nothing has ever opened
        # the file) - the only safe outcome is removal, not clearing
        # pending_scan and serving the raw file.
        from urbanlens.dashboard.tasks import process_image_upload

        image = self._upload()
        with patch("urbanlens.dashboard.tasks._process_photo_upload", return_value=None):
            result = process_image_upload.apply(args=(image.pk,))

        self.assertFalse(result.get())
        self.assertFalse(Image.objects.filter(pk=image.pk).exists(), "an unprocessable pending upload must be removed, not left visible raw")

    def test_permanent_failure_notifies_the_uploader(self) -> None:
        from urbanlens.dashboard.models.notifications.meta import NotificationType
        from urbanlens.dashboard.models.notifications.model import NotificationLog
        from urbanlens.dashboard.tasks import process_image_upload

        image = self._upload()
        with patch("urbanlens.dashboard.tasks._process_photo_upload", return_value=None):
            process_image_upload.apply(args=(image.pk,))

        notification = NotificationLog.objects.filter(profile=self.owner, notification_type=NotificationType.PHOTO_UPLOAD_FAILED).first()
        self.assertIsNotNone(notification)

    def test_permanent_failure_on_a_legacy_already_cleared_row_does_not_delete_it(self) -> None:
        # A backfill/manual re-enqueue of an old row (pending_scan already
        # False - never part of the raw-upload window) hitting the same
        # "cannot open the file" failure is a different problem entirely
        # (the file went missing well after the row was already safe to
        # serve). Deleting it would be a new, unrelated regression - the
        # pre-pending_scan degrade (log and leave it alone) still applies.
        from urbanlens.dashboard.tasks import process_image_upload

        image = self._upload()
        Image.objects.filter(pk=image.pk).update(pending_scan=False)
        with patch("urbanlens.dashboard.tasks._process_photo_upload", return_value=None):
            result = process_image_upload.apply(args=(image.pk,))

        self.assertFalse(result.get())
        self.assertTrue(Image.objects.filter(pk=image.pk).exists())

    def test_rejecting_the_original_also_rejects_a_pending_dedup_sibling(self) -> None:
        # attach_deduped_copy copies pending_scan from its original, and
        # nothing but _sync_deduped_siblings (which only runs on the
        # *success* path) ever revisits a sibling - so if the original is
        # rejected instead, the sibling has to be handled here or it is
        # stuck pending_scan=True forever with no path to clearing.
        from urbanlens.dashboard.services.photos.uploads import attach_deduped_copy
        from urbanlens.dashboard.tasks import process_image_upload

        image = self._upload()
        other_pin = baker.make(Pin, profile=self.owner, location=baker.make(Location))
        sibling = attach_deduped_copy(image, other_pin, self.owner, caption="")
        self.assertTrue(sibling.pending_scan)

        with patch("urbanlens.dashboard.tasks._process_photo_upload", return_value=None):
            process_image_upload.apply(args=(image.pk,))

        self.assertFalse(Image.objects.filter(pk=image.pk).exists())
        self.assertFalse(Image.objects.filter(pk=sibling.pk).exists())


class DedupedCopyInheritsPendingScanTests(TestCase):
    """A dedup sibling must not be a side door around its original's pending_scan.

    ``attach_deduped_copy`` points a new row at the *same stored file* as an
    earlier upload by the same profile, without going through
    ``process_image_upload`` itself. If that original is still ``pending_scan``
    - its stored file is still the uploader's raw bytes, not yet stripped - a
    sibling created into a *different* pin/wiki must not be immediately visible
    there while pointing at those same raw bytes; that would let a second
    upload of identical bytes bypass the very gate the first upload is subject
    to. Nothing but ``tasks._sync_deduped_siblings`` (run when the *original*
    finishes processing) ever revisits a dedup sibling, so that has to be what
    clears it too.
    """

    def test_a_copy_of_an_already_processed_original_is_not_pending(self) -> None:
        from urbanlens.dashboard.services.photos.uploads import attach_deduped_copy

        owner = baker.make(Profile)
        existing = baker.make(Image, profile=owner, image="pin_images/a/b.jpg", checksum="a" * 64, pending_scan=False)
        copy = attach_deduped_copy(existing, owner, owner, caption="")
        self.assertFalse(copy.pending_scan)

    def test_a_copy_of_a_still_pending_original_is_also_pending(self) -> None:
        from urbanlens.dashboard.services.photos.uploads import attach_deduped_copy

        owner = baker.make(Profile)
        existing = baker.make(Image, profile=owner, image="pin_images/a/b.jpg", checksum="a" * 64, pending_scan=True)
        copy = attach_deduped_copy(existing, owner, owner, caption="")
        self.assertTrue(copy.pending_scan)

    def test_processing_the_original_clears_pending_scan_on_its_siblings_too(self) -> None:
        from urbanlens.dashboard.services.photos.uploads import attach_deduped_copy
        from urbanlens.dashboard.tasks import _sync_deduped_siblings

        owner = baker.make(Profile)
        original = baker.make(Image, profile=owner, image="pin_images/a/b.jpg", checksum="a" * 64, pending_scan=True)
        sibling = attach_deduped_copy(original, owner, owner, caption="")
        self.assertTrue(sibling.pending_scan)

        original.pending_scan = False
        original.image = "pin_images/a/processed.webp"
        _sync_deduped_siblings(original)

        sibling.refresh_from_db()
        self.assertFalse(sibling.pending_scan)


class LegacyRowsDefaultToNotPendingTests(DjangoTestCase):
    """Every non-upload row (baker fixtures, materialized/shared copies) is unaffected."""

    def test_baker_default_is_not_pending(self) -> None:
        image = baker.make(Image)
        self.assertFalse(image.pending_scan)
