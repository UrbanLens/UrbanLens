"""Merging a pin must not destroy its albums, overlays, or custom layers.

``merge_pins`` reassigns the loser's relations and then deletes it, and its
module docstring states that "every relation FK'd to Pin falls into one of three
buckets". Three CASCADE relations were in none of them - ``Album.parent_pin``,
``MapImageOverlay.parent_pin`` and ``CustomLayer.parent_pin`` - so the delete
took them with it. Measured before the fix: an album and an overlay on the loser
both returned ``exists() == False`` afterwards.

This is drift rather than a decision. ``pin_merge`` was added 2026-08-02; Album
landed 2026-08-05 and MapImageOverlay 2026-08-06, so the module's completeness
claim quietly stopped being true.

The album case is not a plain reassign: ``uq_album_pin_slug`` is unique on
``(parent_pin, slug)``, and two pins each having a "Photos" album is ordinary.
Both hold real images, so neither may be dropped - the loser's album is
re-slugged instead.

The final test is the important one: rather than listing the three models it
knows about, it asserts that *no* CASCADE relation to Pin is left unhandled, so
the next model to grow a ``parent_pin`` fails here instead of silently deleting
user data.
"""

from __future__ import annotations

import inspect
import re

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.album.model import Album
from urbanlens.dashboard.models.floorplans.model import Floorplan, FloorplanFloor, FloorplanMarker
from urbanlens.dashboard.models.images.attachment import ImageAttachment
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.map_overlay.model import MapImageOverlay
from urbanlens.dashboard.models.markup.model import CustomLayer
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.pins import pin_merge
from urbanlens.dashboard.services.pins.pin_merge import merge_pins


class PinMergePreservesAttachmentsTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = Profile.objects.get(user=baker.make("auth.User"))
        self.survivor = baker.make(Pin, profile=self.profile, location=baker.make(Location), name="Survivor")
        self.loser = baker.make(Pin, profile=self.profile, location=baker.make(Location), name="Loser")

    def test_an_album_survives_and_moves_to_the_survivor(self) -> None:
        album = baker.make(Album, profile=self.profile, parent_pin=self.loser, name="Loser album")

        merge_pins(self.survivor, self.loser, self.profile)

        album.refresh_from_db()
        self.assertEqual(album.parent_pin_id, self.survivor.pk)

    def test_an_overlay_survives_and_moves(self) -> None:
        overlay = baker.make(MapImageOverlay, profile=self.profile, parent_pin=self.loser, name="Loser overlay")

        merge_pins(self.survivor, self.loser, self.profile)

        overlay.refresh_from_db()
        self.assertEqual(overlay.parent_pin_id, self.survivor.pk)

    def test_a_custom_layer_survives_and_moves(self) -> None:
        layer = baker.make(CustomLayer, profile=self.profile, parent_pin=self.loser, name="Loser layer")

        merge_pins(self.survivor, self.loser, self.profile)

        layer.refresh_from_db()
        self.assertEqual(layer.parent_pin_id, self.survivor.pk)

    def test_both_albums_survive_a_slug_collision(self) -> None:
        """Two pins each with a "Photos" album is ordinary; both hold real images."""
        kept = baker.make(Album, profile=self.profile, parent_pin=self.survivor, name="Photos")
        moved = baker.make(Album, profile=self.profile, parent_pin=self.loser, name="Photos")
        self.assertEqual(kept.slug, moved.slug, "precondition: the two albums collide on slug")

        merge_pins(self.survivor, self.loser, self.profile)

        self.assertEqual(Album.objects.filter(parent_pin=self.survivor).count(), 2)
        moved.refresh_from_db()
        kept.refresh_from_db()
        self.assertNotEqual(moved.slug, kept.slug, "the moved album kept a colliding slug")

    def test_the_survivors_own_attachments_are_untouched(self) -> None:
        own = baker.make(Album, profile=self.profile, parent_pin=self.survivor, name="Survivor album")

        merge_pins(self.survivor, self.loser, self.profile)

        own.refresh_from_db()
        self.assertEqual(own.parent_pin_id, self.survivor.pk)

    def test_an_image_attachment_survives_and_moves_to_the_survivor(self) -> None:
        image = baker.make(Image, profile=self.profile)
        attachment = baker.make(ImageAttachment, image=image, pin=self.loser)

        merge_pins(self.survivor, self.loser, self.profile)

        attachment.refresh_from_db()
        self.assertEqual(attachment.pin_id, self.survivor.pk)

    def test_a_duplicate_image_attachment_is_dropped_not_left_dangling(self) -> None:
        """The same photo already attached to both pins is one attachment, not a collision."""
        image = baker.make(Image, profile=self.profile)
        kept = baker.make(ImageAttachment, image=image, pin=self.survivor)
        dropped = baker.make(ImageAttachment, image=image, pin=self.loser)

        merge_pins(self.survivor, self.loser, self.profile)

        self.assertTrue(ImageAttachment.objects.filter(pk=kept.pk).exists())
        self.assertFalse(ImageAttachment.objects.filter(pk=dropped.pk).exists())
        self.assertEqual(ImageAttachment.objects.filter(pin=self.survivor, image=image).count(), 1)

    def test_a_floorplan_markers_twin_is_unlinked_not_deleted(self) -> None:
        """Repointing the twin onto survivor would let a later floorplan save silently
        overwrite survivor's name/location, so the marker is unlinked instead - its own
        position/kind/floor data survives, and the editor mints a fresh twin next save."""
        floor = baker.make(FloorplanFloor, floorplan=baker.make(Floorplan, profile=self.profile))
        marker = baker.make(FloorplanMarker, floor=floor, linked_pin=self.loser)

        merge_pins(self.survivor, self.loser, self.profile)

        marker.refresh_from_db()
        self.assertIsNone(marker.linked_pin_id)

    def test_an_existing_twin_on_survivor_is_undisturbed(self) -> None:
        """Survivor is already another marker's twin - unlinking the loser's marker (rather
        than reassigning it onto survivor) never touches that unrelated marker."""
        floor = baker.make(FloorplanFloor, floorplan=baker.make(Floorplan, profile=self.profile))
        existing = baker.make(FloorplanMarker, floor=floor, linked_pin=self.survivor, name="Existing")
        moved = baker.make(FloorplanMarker, floor=floor, linked_pin=self.loser, name="Moved")

        merge_pins(self.survivor, self.loser, self.profile)

        existing.refresh_from_db()
        moved.refresh_from_db()
        self.assertEqual(existing.linked_pin_id, self.survivor.pk)
        self.assertIsNone(moved.linked_pin_id)

    def test_no_cascade_relation_to_pin_is_left_unhandled(self) -> None:
        """The completeness arm: the next model with a parent_pin fails here.

        A relation that CASCADEs from Pin and is never mentioned in the merge is
        deleted along with the loser. Listing the three models found today would
        not catch the fourth.
        """
        source = inspect.getsource(pin_merge)
        unhandled = sorted(
            rel.related_model.__name__
            for rel in Pin._meta.related_objects
            # M2M relations expose no on_delete; their through rows carry no user
            # data of their own, and the one M2M that matters (labels) is merged.
            if getattr(getattr(rel.field.remote_field, "on_delete", None), "__name__", "") == "CASCADE"
            and not re.search(rf"\b{rel.related_model.__name__}\b", source)
        )

        self.assertEqual(
            unhandled,
            [],
            "these CASCADE relations are destroyed when a pin is merged away - handle or document them",
        )
