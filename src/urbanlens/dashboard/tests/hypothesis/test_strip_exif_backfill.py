"""The backfill that scrubs photos stored before EXIF was stripped on upload."""

from __future__ import annotations

import io
from pathlib import Path
import shutil
import tempfile
from unittest import mock

from botocore.exceptions import ClientError
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db.models.fields.files import FieldFile
from django.test import override_settings
from model_bakery import baker
from PIL import Image as PILImage

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.achievements.model import Achievement
from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.labels.meta import KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trips.model import Trip, TripComment

_MAKE_TAG = 0x010F
_GPS_IFD = 0x8825


def _jpeg_with_exif() -> bytes:
    img = PILImage.new("RGB", (320, 240), (10, 20, 30))
    exif = PILImage.Exif()
    exif[_MAKE_TAG] = "ACME Cameras"
    gps = exif.get_ifd(_GPS_IFD)
    gps[1] = "N"
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", exif=exif.tobytes())
    return buffer.getvalue()


def _jpeg_with_comment() -> bytes:
    buffer = io.BytesIO()
    PILImage.new("RGB", (320, 240), (10, 20, 30)).save(buffer, format="JPEG", comment=b"Old Mill House")
    return buffer.getvalue()


def _refusing_to_open(name: str):
    """FieldFile.open, except that the object store turns away a read of *name*, as the S3 backend reports it."""
    original = FieldFile.open

    def open_(field_file: FieldFile, mode: str = "rb") -> FieldFile:
        if field_file.name == name:
            raise ClientError(
                {"Error": {"Code": "SlowDown", "Message": "busy"}, "ResponseMetadata": {"HTTPStatusCode": 503}},
                "HeadObject",
            )
        return original(field_file, mode)

    return open_


class StripExifBackfillTests(TestCase):
    def setUp(self) -> None:
        self._media_root = tempfile.mkdtemp(prefix="ul_exif_backfill_")
        self.addCleanup(shutil.rmtree, self._media_root, ignore_errors=True)
        (Path(self._media_root) / "pin_images").mkdir(parents=True, exist_ok=True)
        overrides = override_settings(MEDIA_ROOT=self._media_root)
        overrides.enable()
        self.addCleanup(overrides.disable)

    def _stored_image(self, data: bytes, name: str = "old.jpg") -> Image:
        image = baker.make(Image, image=None, exif_data=None)
        image.image.save(name, ContentFile(data), save=True)
        return image

    def _recorded(self, image: Image) -> dict:
        image.refresh_from_db()
        self.assertIsInstance(image.exif_data, dict, "no EXIF was recorded on the row")
        assert isinstance(image.exif_data, dict)
        return image.exif_data

    def _read_back(self, image: Image) -> PILImage.Image:
        image.refresh_from_db()
        with image.image.open("rb") as handle:
            return PILImage.open(io.BytesIO(handle.read()))

    def test_the_block_leaves_the_file(self) -> None:
        image = self._stored_image(_jpeg_with_exif())

        call_command("strip_exif_from_stored_photos")

        out = self._read_back(image)
        self.assertIsNone(out.getexif().get(_MAKE_TAG), "the camera make is still in the stored file")
        self.assertFalse(out.getexif().get_ifd(_GPS_IFD), "the GPS IFD is still in the stored file")

    def test_the_values_are_kept_on_the_row(self) -> None:
        image = self._stored_image(_jpeg_with_exif())

        call_command("strip_exif_from_stored_photos")

        self.assertEqual(self._recorded(image).get("Make"), "ACME Cameras")

    def test_an_already_recorded_row_is_not_overwritten(self) -> None:
        """A photo whose EXIF was captured on upload keeps what it captured."""
        image = self._stored_image(_jpeg_with_exif())
        Image.objects.filter(pk=image.pk).update(exif_data={"Make": "recorded earlier"})

        call_command("strip_exif_from_stored_photos")

        self.assertEqual(self._recorded(image).get("Make"), "recorded earlier")

    def test_a_dry_run_changes_nothing(self) -> None:
        image = self._stored_image(_jpeg_with_exif())

        call_command("strip_exif_from_stored_photos", "--dry-run")

        out = self._read_back(image)
        self.assertEqual(out.getexif().get(_MAKE_TAG), "ACME Cameras", "--dry-run rewrote the file")
        image.refresh_from_db()
        self.assertIsNone(image.exif_data, "--dry-run wrote to the row")

    def test_a_photo_still_pending_is_left_to_its_upload_task(self) -> None:
        """The upload task is rewriting that file, and two writers can leave the row naming a deleted one."""
        image = self._stored_image(_jpeg_with_exif())
        Image.objects.filter(pk=image.pk).update(pending_scan=True)
        original_name = image.image.name

        call_command("strip_exif_from_stored_photos")

        image.refresh_from_db()
        self.assertEqual(image.image.name, original_name)

    def test_gps_is_recorded_for_someone_who_keeps_location_on(self) -> None:
        """Guards the test below: it would pass if the GPS block were never recorded under that key."""
        image = self._stored_image(_jpeg_with_exif())

        call_command("strip_exif_from_stored_photos")

        self.assertIn("GPSInfo", self._recorded(image))

    def test_gps_is_not_recorded_for_someone_who_turned_location_off(self) -> None:
        owner = baker.make(User).profile
        Profile.objects.filter(pk=owner.pk).update(track_pin_visits=False)
        image = self._stored_image(_jpeg_with_exif())
        Image.objects.filter(pk=image.pk).update(profile=owner)

        call_command("strip_exif_from_stored_photos")

        recorded = self._recorded(image)
        self.assertEqual(recorded.get("Make"), "ACME Cameras")
        self.assertNotIn("GPSInfo", recorded)

    def test_a_photo_with_no_exif_is_still_reencoded(self) -> None:
        """A comment, XMP or IPTC can name the place as well as EXIF can, so no stored photo is kept as it was."""
        image = self._stored_image(_jpeg_with_comment(), name="commented.jpg")
        original_name = image.image.name

        call_command("strip_exif_from_stored_photos")

        image.refresh_from_db()
        self.assertNotEqual(image.image.name, original_name)
        with image.image.open("rb") as handle:
            self.assertNotIn(b"Old Mill House", handle.read())

    def test_a_photo_the_object_store_refuses_does_not_stop_the_rest(self) -> None:
        refused = self._stored_image(_jpeg_with_exif(), name="refused.jpg")
        later = self._stored_image(_jpeg_with_exif(), name="later.jpg")
        stderr = io.StringIO()

        with mock.patch.object(FieldFile, "open", _refusing_to_open(refused.image.name)):
            call_command("strip_exif_from_stored_photos", stderr=stderr)

        self.assertIn("ClientError", stderr.getvalue())
        self.assertIsNone(self._read_back(later).getexif().get(_MAKE_TAG))


class OtherStoredImagesBackfillTests(TestCase):
    """Comment images, custom icons and avatars stored before uploads of them were re-encoded (P119)."""

    def setUp(self) -> None:
        self._media_root = tempfile.mkdtemp(prefix="ul_other_backfill_")
        self.addCleanup(shutil.rmtree, self._media_root, ignore_errors=True)
        overrides = override_settings(MEDIA_ROOT=self._media_root)
        overrides.enable()
        self.addCleanup(overrides.disable)
        self.profile = baker.make(User).profile

    def _upload(self, data: bytes | None = None, name: str = "old.jpg") -> SimpleUploadedFile:
        return SimpleUploadedFile(name, _jpeg_with_comment() if data is None else data, content_type="image/jpeg")

    def _carries_the_comment(self, stored: FieldFile) -> bool:
        with stored.open("rb") as handle:
            return b"Old Mill House" in handle.read()

    def _comment(self, **fields: object) -> Comment:
        return Comment.objects.create(
            pin=baker.make(Pin, profile=self.profile), profile=self.profile, text="look", image=self._upload(), **fields
        )

    def test_the_fixture_carries_what_the_backfill_must_remove(self) -> None:
        self.assertTrue(self._carries_the_comment(self._comment().image))

    def test_a_published_comment_image_is_reencoded(self) -> None:
        comment = self._comment()

        call_command("strip_exif_from_stored_photos")

        comment.refresh_from_db()
        self.assertFalse(self._carries_the_comment(comment.image))

    def test_a_published_trip_comment_image_is_reencoded(self) -> None:
        comment = TripComment.objects.create(
            trip=baker.make(Trip, creator=self.profile), author=self.profile, text="look", image=self._upload()
        )

        call_command("strip_exif_from_stored_photos")

        comment.refresh_from_db()
        self.assertFalse(self._carries_the_comment(comment.image))

    def test_a_label_icon_is_reencoded(self) -> None:
        label = baker.make(Label, profile=self.profile, kind=KIND_TAG, name="Urbex")
        Label.objects.filter(pk=label.pk).update(
            custom_icon=default_storage.save("label_icons/old.jpg", self._upload())
        )

        call_command("strip_exif_from_stored_photos")

        label.refresh_from_db()
        self.assertFalse(self._carries_the_comment(label.custom_icon))

    def test_a_pin_and_an_achievement_icon_are_reencoded(self) -> None:
        pin = baker.make(Pin, profile=self.profile)
        achievement = Achievement.objects.create(name="ZzOld Award", metric="photos_uploaded", threshold=1)
        Pin.objects.filter(pk=pin.pk).update(
            custom_icon=default_storage.save("pin_custom_icons/old.jpg", self._upload())
        )
        Achievement.objects.filter(pk=achievement.pk).update(
            custom_icon=default_storage.save("achievement_icons/old.jpg", self._upload())
        )

        call_command("strip_exif_from_stored_photos")

        for row in (pin, achievement):
            with self.subTest(type(row).__name__):
                row.refresh_from_db()
                self.assertFalse(self._carries_the_comment(row.custom_icon))

    def test_an_avatar_is_reencoded(self) -> None:
        Profile.objects.filter(pk=self.profile.pk).update(
            avatar=default_storage.save("avatars/old.jpg", self._upload())
        )

        call_command("strip_exif_from_stored_photos")

        self.profile.refresh_from_db()
        self.assertFalse(self._carries_the_comment(self.profile.avatar))

    def test_a_generated_emoji_avatar_is_left_alone(self) -> None:
        for stored_as in ("avatars/emoji_1.svg", "avatars/emoji_1_aB3dE9x.svg"):
            with self.subTest(stored_as):
                name = default_storage.save(stored_as, ContentFile(b"<svg xmlns='http://www.w3.org/2000/svg'/>"))
                Profile.objects.filter(pk=self.profile.pk).update(avatar=name)

                call_command("strip_exif_from_stored_photos")

                self.profile.refresh_from_db()
                self.assertEqual(self.profile.avatar.name, name)

    def test_an_uploaded_svg_avatar_is_not_taken_for_a_generated_one(self) -> None:
        """An SVG stored before uploads were allowlisted is someone's markup, not the site's template."""
        name = default_storage.save(
            "avatars/drawn.svg", ContentFile(b"<svg xmlns='http://www.w3.org/2000/svg' onload='alert(1)'/>")
        )
        Profile.objects.filter(pk=self.profile.pk).update(avatar=name)

        call_command("strip_exif_from_stored_photos")

        self.profile.refresh_from_db()
        self.assertFalse(self.profile.avatar)
        self.assertFalse(default_storage.exists(name))

    def test_a_published_comment_image_that_cannot_be_read_right_now_is_kept(self) -> None:
        comment = self._comment()
        name = comment.image.name

        with mock.patch.object(FieldFile, "open", side_effect=OSError("storage unavailable")):
            call_command("strip_exif_from_stored_photos", stderr=io.StringIO())

        comment.refresh_from_db()
        self.assertEqual(comment.image.name, name)

    def test_a_comment_image_the_object_store_refuses_does_not_stop_the_rest(self) -> None:
        comment = self._comment()
        name = comment.image.name
        Profile.objects.filter(pk=self.profile.pk).update(
            avatar=default_storage.save("avatars/old.jpg", self._upload())
        )
        stderr = io.StringIO()

        with mock.patch.object(FieldFile, "open", _refusing_to_open(name)):
            call_command("strip_exif_from_stored_photos", stderr=stderr)

        self.assertIn("ClientError", stderr.getvalue())
        comment.refresh_from_db()
        self.assertEqual(comment.image.name, name)
        self.profile.refresh_from_db()
        self.assertFalse(self._carries_the_comment(self.profile.avatar))

    def test_a_comment_still_pending_is_left_to_its_scan_task(self) -> None:
        comment = self._comment(pending_scan=True)
        original_name = comment.image.name

        call_command("strip_exif_from_stored_photos")

        comment.refresh_from_db()
        self.assertEqual(comment.image.name, original_name)

    def test_a_published_comment_whose_image_cannot_be_decoded_keeps_its_text(self) -> None:
        """The comment has been seen already; losing the image is enough, and rejecting it would delete the text too."""
        comment = Comment.objects.create(
            pin=baker.make(Pin, profile=self.profile),
            profile=self.profile,
            text="look",
            image=self._upload(b"\xff\xd8\xff\xe0 not really a jpeg"),
        )

        call_command("strip_exif_from_stored_photos")

        comment.refresh_from_db()
        self.assertFalse(comment.image)
