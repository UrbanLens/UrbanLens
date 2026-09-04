"""A photo belongs to everything that cites it, and outlives any one of them.

`Image` carries a single `pin` and a single `wiki` column, which says a photo
belongs to at most one of each. Child pins make that false in ordinary use: a
photo of a building is a photo of the building's pin and of the parcel pin above
it. `ImageAttachment` is a row per attachment instead.

The durability half matters more. A floorplan cites a photo, and deleting that
photo from somebody's media used to delete the citation with it - the reference
row cascaded away, taking the caption and the thing it was attached to.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import TestCase
from model_bakery import baker

from urbanlens.dashboard.models.floorplans.model import Floorplan, FloorplanReference
from urbanlens.dashboard.models.images.attachment import ImageAttachment
from urbanlens.dashboard.models.images.model import Image, ImageSource
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.photos.attachment import (
    attach_to_pin,
    attach_to_wiki,
    collect_if_unreferenced,
    reference_count,
)


def _image(profile, *, source: str = ImageSource.UPLOAD, **kwargs) -> Image:
    """A stored image row with real bytes behind it."""
    return Image.objects.create(
        image=SimpleUploadedFile("photo.jpg", b"not-a-real-jpeg", content_type="image/jpeg"),
        profile=profile,
        source=source,
        **kwargs,
    )


class ImageAttachmentTests(TestCase):
    """One photo, many places."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile
        self.building = baker.make(Pin, profile=self.profile, parent_pin=None)
        self.parcel = baker.make(Pin, profile=self.profile, parent_pin=None)

    def test_one_photo_attaches_to_several_pins(self) -> None:
        """The case child pins make ordinary: a building photo is also the parcel's."""
        image = _image(self.profile)

        attach_to_pin(image, self.building, added_by=self.profile)
        attach_to_pin(image, self.parcel, added_by=self.profile)

        self.assertEqual(
            set(ImageAttachment.objects.filter(image=image).values_list("pin_id", flat=True)),
            {self.building.pk, self.parcel.pk},
        )

    def test_attaching_twice_is_the_same_attachment(self) -> None:
        image = _image(self.profile)

        first = attach_to_pin(image, self.building)
        second = attach_to_pin(image, self.building)

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(ImageAttachment.objects.filter(image=image).count(), 1)

    def test_a_row_must_attach_to_exactly_one_thing(self) -> None:
        """Both set is counted twice by every reference count; neither keeps a
        photo alive while attaching it to nothing, which is what would defeat
        collecting unreferenced photos."""
        image = _image(self.profile)

        with self.assertRaises(IntegrityError), transaction.atomic():
            ImageAttachment.objects.create(image=image, pin=None, wiki=None)

    def test_deleting_a_pin_takes_its_attachment_and_nothing_else(self) -> None:
        image = _image(self.profile)
        attach_to_pin(image, self.building)
        attach_to_pin(image, self.parcel)

        self.building.delete()

        self.assertTrue(Image.objects.filter(pk=image.pk).exists(), "deleting a pin deleted the photo itself")
        self.assertEqual(
            list(ImageAttachment.objects.filter(image=image).values_list("pin_id", flat=True)), [self.parcel.pk]
        )


class FloorplanReferenceOutlivesItsPhotoTests(TestCase):
    """The requirement this whole change exists for."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile
        self.floorplan = Floorplan.objects.create(profile=self.profile)

    def test_deleting_the_photo_keeps_the_citation(self) -> None:
        image = _image(self.profile)
        reference = FloorplanReference.objects.create(
            floorplan=self.floorplan,
            image=image,
            title="South elevation",
            url="https://example.test/south.jpg",
            description="Taken from the car park",
        )

        image.delete()

        reference.refresh_from_db()
        self.assertIsNone(reference.image_id, "the reference should survive with no image")
        # Everything it said about the photo, and the address it can still be
        # shown from: a reference that loses its stored copy degrades to a
        # URL-backed one rather than vanishing.
        self.assertEqual(reference.title, "South elevation")
        self.assertEqual(reference.url, "https://example.test/south.jpg")
        self.assertEqual(reference.description, "Taken from the car park")


class ImageCollectionTests(TestCase):
    """What may be collected, and - more importantly - what may not."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile
        self.pin = baker.make(Pin, profile=self.profile, parent_pin=None)
        self.floorplan = Floorplan.objects.create(profile=self.profile)

    def test_a_fetched_photo_nothing_cites_is_collected(self) -> None:
        image = _image(self.profile, source=ImageSource.LINKED_URL, source_media_url="https://example.test/a.jpg")

        self.assertTrue(collect_if_unreferenced(image))
        self.assertFalse(Image.objects.filter(pk=image.pk).exists())

    def test_an_uploaded_photo_is_never_collected(self) -> None:
        """It is its owner's library whether or not anything points at it.

        Collecting on "no attachments" would delete people's own pictures the
        moment they removed the last pin that used them.
        """
        image = _image(self.profile, source=ImageSource.UPLOAD)

        self.assertFalse(collect_if_unreferenced(image))
        self.assertTrue(Image.objects.filter(pk=image.pk).exists())

    def test_a_floorplan_citation_keeps_a_fetched_photo_alive(self) -> None:
        image = _image(self.profile, source=ImageSource.LINKED_URL)
        FloorplanReference.objects.create(floorplan=self.floorplan, image=image)

        self.assertFalse(collect_if_unreferenced(image))
        self.assertTrue(Image.objects.filter(pk=image.pk).exists())

    def test_an_attachment_keeps_a_fetched_photo_alive(self) -> None:
        image = _image(self.profile, source=ImageSource.LINKED_URL)
        attach_to_pin(image, self.pin)

        self.assertFalse(collect_if_unreferenced(image))

    def test_reference_count_sees_every_kind_of_citation(self) -> None:
        image = _image(self.profile, source=ImageSource.LINKED_URL)
        self.assertEqual(reference_count(image), 0)

        attach_to_pin(image, self.pin)
        self.assertEqual(reference_count(image), 1)

        FloorplanReference.objects.create(floorplan=self.floorplan, image=image)
        self.assertEqual(reference_count(image), 2)


class SourceUrlPairTests(TestCase):
    """Two addresses that rot independently, and which one each caller wants."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile

    def test_a_person_is_sent_to_the_page_and_a_refetch_goes_to_the_file(self) -> None:
        image = _image(
            self.profile,
            source_url="https://example.test/photos/123",
            source_media_url="https://cdn.example.test/123_o.jpg",
        )

        self.assertEqual(image.attribution_url, "https://example.test/photos/123")
        self.assertEqual(image.origin_media_url, "https://cdn.example.test/123_o.jpg")

    def test_each_falls_back_to_the_other(self) -> None:
        page_only = _image(self.profile, source_url="https://example.test/photos/123")
        file_only = _image(self.profile, source_media_url="https://cdn.example.test/123_o.jpg")

        self.assertEqual(page_only.origin_media_url, "https://example.test/photos/123")
        self.assertEqual(file_only.attribution_url, "https://cdn.example.test/123_o.jpg")

    def test_neither_set_is_empty_rather_than_none(self) -> None:
        image = _image(self.profile)

        self.assertEqual(image.attribution_url, "")
        self.assertEqual(image.origin_media_url, "")
