"""Accepting a share must copy an image faithfully, not reinvent it from defaults.

``create_pin_from_share`` builds the recipient's ``Image`` rows field by field. Any
field it forgets silently takes the model default rather than the source row's value,
and two of those defaults are wrong in a way nobody would see in review: ``source``
defaults to ``UPLOAD`` and ``media_type`` defaults to ``PHOTO``.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image, ImageSource, MediaKind, QuotaExemption
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_share.model import PinShare, PinShareStatus
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.sharing.pin_sharing import create_pin_from_share


class SharedImageCopyFidelityTests(TestCase):
    """Fields the copy omits fall back to defaults that misdescribe the photo."""

    def setUp(self):
        super().setUp()
        self.sender = self._profile("share-sender")
        self.recipient = self._profile("share-recipient")
        self.location = Location.objects.create(latitude=40.5, longitude=-73.5)
        self.pin = Pin.objects.create(profile=self.sender, location=self.location, name="Shared place")

    @staticmethod
    def _profile(username: str) -> Profile:
        user = User.objects.create_user(username=username)
        profile, _ = Profile.objects.get_or_create(user=user)
        return profile

    def _share_image(self, **image_kwargs) -> Image:
        """Share the pin with one image attached, accept it, and return the copy."""
        image = Image.objects.create(pin=self.pin, location=self.location, profile=self.sender, image="photos/original.jpg", **image_kwargs)
        share = PinShare.objects.create(
            pin=self.pin, location=self.location, from_profile=self.sender, to_profile=self.recipient, status=PinShareStatus.PENDING,
        )
        share.images.set([image])

        create_pin_from_share(share)

        copied = Image.objects.filter(profile=self.recipient).first()
        self.assertIsNotNone(copied, "accepting the share created no image for the recipient")
        return copied

    def test_an_external_photo_does_not_become_the_recipients_own_upload(self):
        # ImageSource drives the Media section's per-source tabs, so a Wikimedia
        # photo filed as an upload is both misattributed and in the wrong tab.
        copied = self._share_image(source=ImageSource.WIKIMEDIA)
        self.assertEqual(copied.source, ImageSource.WIKIMEDIA)

    def test_a_genuine_upload_stays_an_upload(self):
        copied = self._share_image(source=ImageSource.UPLOAD)
        self.assertEqual(copied.source, ImageSource.UPLOAD)

    def test_a_shared_video_is_still_a_video(self):
        # media_type decides player vs viewer vs <img>, so a video copied as a
        # photo renders as a broken image.
        copied = self._share_image(media_type=MediaKind.VIDEO)
        self.assertEqual(copied.media_type, MediaKind.VIDEO)

    def test_the_copy_belongs_to_the_recipient(self):
        copied = self._share_image(source=ImageSource.YELP)
        self.assertEqual(copied.profile_id, self.recipient.pk)

    def test_attribution_travels_with_the_copy(self):
        copied = self._share_image(source=ImageSource.WIKIMEDIA, author="A. Photographer", copyright="CC BY-SA 4.0")
        self.assertEqual(copied.author, "A. Photographer")
        self.assertEqual(copied.copyright, "CC BY-SA 4.0")

    def test_the_senders_row_is_untouched(self):
        self._share_image(source=ImageSource.WIKIMEDIA)
        original = Image.objects.get(profile=self.sender)
        self.assertEqual(original.source, ImageSource.WIKIMEDIA)
        self.assertEqual(original.pin_id, self.pin.pk)

    def test_the_copy_is_exempt_from_the_recipients_storage_quota(self):
        """The copy points at the sender's already-stored file (see this module's
        docstring and ``image=image.image.name`` in create_pin_from_share) - it
        occupies no storage of the recipient's own to charge quota for."""
        from urbanlens.dashboard.services.media.storage import get_storage_used_bytes

        copied = self._share_image(file_size=5_000_000)
        self.assertEqual(copied.quota_exempt_reason, QuotaExemption.SHARED_COPY)
        self.assertEqual(get_storage_used_bytes(self.recipient), 0)


class SharedPinCarriesTheSiteNotTheOwnerTests(TestCase):
    """Accepting a share gives you the place, not the person's account of it.

    A share carries what is true about the site - its dates, and what was
    observed there. It does not carry how somebody chose to decorate their own
    pin, and it does not carry their labels: a Label belongs to one profile, so
    copying them hung the sharer's rows off the recipient's pin, showing one
    person's private organising scheme to another and leaving the recipient
    holding references they cannot manage.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.sender = baker.make(User).profile
        self.recipient = baker.make(User).profile
        self.location = baker.make(Location, latitude=41.7361, longitude=-73.9361)
        self.pin = baker.make(
            Pin,
            profile=self.sender,
            location=self.location,
            parent_pin=None,
            detail_bg_color="#123456",
            cameras=True,
            locked=True,
            date_abandoned="1994-06-15",
        )

    def _accept(self) -> Pin:
        from urbanlens.dashboard.models.pin_share.model import PinShare
        from urbanlens.dashboard.services.sharing.pin_sharing import create_pin_from_share

        share = PinShare.objects.create(pin=self.pin, location=self.location, from_profile=self.sender, to_profile=self.recipient)
        return create_pin_from_share(share)

    def test_what_was_observed_at_the_site_travels(self) -> None:
        """The positive control - this must not pass by copying nothing."""
        new_pin = self._accept()

        self.assertTrue(new_pin.cameras)
        self.assertTrue(new_pin.locked)
        self.assertEqual(str(new_pin.date_abandoned), "1994-06-15")

    def test_the_senders_personal_notes_never_travel(self) -> None:
        """The description is the owner's own account of the place, and nothing in
        the product lets somebody agree to pass it on. Absent a way to ask, no."""
        self.pin.description = "zzq-my-private-notes-about-this-place"
        self.pin.save(update_fields=["description"])

        new_pin = self._accept()

        self.assertNotIn("zzq-my-private-notes", new_pin.description or "", "the recipient received the sender's personal notes")

    def test_the_senders_styling_does_not_travel(self) -> None:
        new_pin = self._accept()

        self.assertNotEqual(new_pin.detail_bg_color, "#123456", "the recipient inherited the sender's pin styling")

    def test_the_senders_own_view_of_the_place_does_not_travel(self) -> None:
        """Icon, colour and priority are how one person marked a place for
        themselves, not anything true about it."""
        self.pin.icon = "zzq-custom-icon"
        self.pin.color = "#abcdef"
        self.pin.priority = 5
        self.pin.save(update_fields=["icon", "color", "priority"])

        new_pin = self._accept()

        self.assertNotEqual(new_pin.icon, "zzq-custom-icon", "the recipient inherited the sender's icon")
        self.assertNotEqual(new_pin.color, "#abcdef", "the recipient inherited the sender's colour")
        self.assertNotEqual(new_pin.priority, 5, "the recipient inherited the sender's priority")

    def test_the_senders_own_name_for_the_pin_does_not_travel(self) -> None:
        """What somebody called their own pin says more about them than the place."""
        self.pin.name = "zzq-my-old-school"
        self.pin.save(update_fields=["name"])
        self.location.official_name = "Hudson River State Hospital"
        self.location.save(update_fields=["official_name"])

        new_pin = self._accept()

        self.assertNotIn("zzq-my-old-school", new_pin.name or "", "the recipient inherited the sender's name for the pin")
        self.assertEqual(new_pin.name, "Hudson River State Hospital")

    def test_a_deliberate_shared_name_is_used(self) -> None:
        """The consent path: the sharer names the share themselves."""
        from urbanlens.dashboard.models.pin_share.model import PinShare
        from urbanlens.dashboard.services.sharing.pin_sharing import create_pin_from_share

        share = PinShare.objects.create(pin=self.pin, location=self.location, from_profile=self.sender, to_profile=self.recipient, shared_name="The old hospital")

        self.assertEqual(create_pin_from_share(share).name, "The old hospital")

    def test_a_shared_photo_keeps_where_and_when_but_not_the_caption(self) -> None:
        """Dates and coordinates place the photo; the caption is what the sharer
        wrote about it, and exif_data/original_filename are the file behind it."""
        photo = Image.objects.create(
            image=SimpleUploadedFile("PXL_20260709_123456.jpg", b"not-a-real-jpeg", content_type="image/jpeg"),
            profile=self.sender,
            pin=self.pin,
            location=self.location,
            caption="zzq-what-i-thought-of-it",
            latitude=41.7361,
            longitude=-73.9361,
            direction=270,
            exif_data={"GPSInfo": {"secret": True}},
        )
        from urbanlens.dashboard.models.pin_share.model import PinShare
        from urbanlens.dashboard.services.sharing.pin_sharing import create_pin_from_share

        share = PinShare.objects.create(pin=self.pin, location=self.location, from_profile=self.sender, to_profile=self.recipient)
        share.images.set([photo])

        new_pin = create_pin_from_share(share)
        copy = Image.objects.get(pin=new_pin)

        self.assertNotIn("zzq-what-i-thought-of-it", copy.caption or "")
        self.assertFalse(copy.exif_data, "the recipient received the sender's photo metadata")
        self.assertFalse(copy.original_filename, "the recipient received the sender's original filename")
        self.assertEqual(float(copy.latitude), 41.7361)
        self.assertEqual(copy.direction, 270)
        # filename_taken_at is a date, like taken_at/latitude/longitude above -
        # it travels even though the filename it was parsed from does not.
        self.assertEqual(copy.filename_taken_at, photo.filename_taken_at)
        self.assertIsNotNone(copy.filename_taken_at)
        self.assertIn(self.sender.username, copy.author or "", "an unattributed photo should say who shared it")

    def test_the_senders_ratings_of_the_place_do_not_travel(self) -> None:
        """vulnerability and danger look like properties of the site and are not:
        they are one person's assessment of it."""
        self.pin.vulnerability = 4
        self.pin.danger = 5
        self.pin.save(update_fields=["vulnerability", "danger"])

        new_pin = self._accept()

        self.assertNotEqual(new_pin.vulnerability, 4, "the recipient inherited the sender's vulnerability rating")
        self.assertNotEqual(new_pin.danger, 5, "the recipient inherited the sender's danger rating")

    def test_the_senders_labels_do_not_travel(self) -> None:
        """Labels are per-profile; the recipient must not end up holding the
        sender's rows."""
        from urbanlens.dashboard.models.labels.model import Label

        label = baker.make(Label, profile=self.sender, name="zzq-senders-private-label")
        self.pin.labels.add(label)

        new_pin = self._accept()

        self.assertEqual(list(new_pin.labels.all()), [], "the sender's labels were attached to the recipient's pin")
