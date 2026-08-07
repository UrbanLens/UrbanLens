"""Adding a photo to an album must survive a concurrent add of the same photo.

``add_images_to_album`` reads which images are already in the album, then inserts the
rest - a check-then-act with no lock. ``uq_album_item`` correctly stops the duplicate
row, but an unguarded insert turns the loser of that race into an ``IntegrityError``
rather than a no-op.

The race is not hypothetical: there are two callers, and one of them is the Celery task
``cache_media_item_into_album``. Celery delivers at least once, so a redelivered task
races both a retry of itself and the user adding the same photo from the picker once it
materialises.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.album.model import Album, AlbumItem
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.photos.albums import add_images_to_album


class AlbumAddRaceTests(TestCase):
    """A duplicate add is a no-op, not a 500."""

    def setUp(self):
        super().setUp()
        user = User.objects.create_user(username="album-race")
        self.profile, _ = Profile.objects.get_or_create(user=user)
        self.location = Location.objects.create(latitude=39.5, longitude=-75.5)
        self.pin = Pin.objects.create(profile=self.profile, location=self.location, name="Album host")
        self.album = Album.objects.create(parent_pin=self.pin, profile=self.profile, name="Ruins", slug="ruins")
        self.image = Image.objects.create(pin=self.pin, location=self.location, profile=self.profile, image="photos/a.jpg")
        self.other = Image.objects.create(pin=self.pin, location=self.location, profile=self.profile, image="photos/b.jpg")

    def _add_with_stale_read(self, images: list[Image]) -> int:
        """Run the add as if another process inserted between our read and our write.

        Patching ``for_album`` to see nothing reproduces exactly that window without
        needing real concurrency, which a single-connection test cannot produce.
        """
        with mock.patch.object(AlbumItem.objects, "for_album", return_value=AlbumItem.objects.none()):
            return add_images_to_album(self.album, images, self.profile)

    def test_a_duplicate_insert_does_not_raise(self):
        AlbumItem.objects.create(album=self.album, image=self.image, order=0)

        added = self._add_with_stale_read([self.image])

        self.assertEqual(added, 0)

    def test_the_membership_is_not_duplicated(self):
        AlbumItem.objects.create(album=self.album, image=self.image, order=0)

        self._add_with_stale_read([self.image])

        self.assertEqual(AlbumItem.objects.filter(album=self.album, image=self.image).count(), 1)

    def test_the_photos_that_are_new_still_get_added(self):
        # Losing the race on one photo must not discard the rest of the batch.
        AlbumItem.objects.create(album=self.album, image=self.image, order=0)

        added = self._add_with_stale_read([self.image, self.other])

        self.assertEqual(added, 1)
        self.assertTrue(AlbumItem.objects.filter(album=self.album, image=self.other).exists())

    def test_the_ordinary_path_is_unaffected(self):
        added = add_images_to_album(self.album, [self.image, self.other], self.profile)

        self.assertEqual(added, 2)
        self.assertEqual(AlbumItem.objects.filter(album=self.album).count(), 2)

    def test_adding_the_same_photo_twice_in_sequence_reports_it_once(self):
        self.assertEqual(add_images_to_album(self.album, [self.image], self.profile), 1)
        self.assertEqual(add_images_to_album(self.album, [self.image], self.profile), 0)

    def test_new_items_are_appended_after_existing_ones(self):
        add_images_to_album(self.album, [self.image], self.profile)
        add_images_to_album(self.album, [self.other], self.profile)

        orders = list(AlbumItem.objects.filter(album=self.album).order_by("order").values_list("image_id", "order"))
        self.assertEqual([image_id for image_id, _ in orders], [self.image.pk, self.other.pk])
