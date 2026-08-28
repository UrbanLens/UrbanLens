"""Mutation undo/redo: pin moves, labels, aliases, albums, photo metadata, and the stack API."""

from __future__ import annotations

import json

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.labels import ensure_label
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.album.model import Album, AlbumItem
from urbanlens.dashboard.models.aliases.model import PinAlias
from urbanlens.dashboard.models.undo import UndoAction
from urbanlens.dashboard.services.media.images import apply_image_map_update
from urbanlens.dashboard.services.photos.albums import add_images_to_album
from urbanlens.dashboard.services.pins.pin_edit import move_pin_to_coordinates
from urbanlens.dashboard.services.pins.pin_subresources import create_pin_alias
from urbanlens.dashboard.services.undo.mutations import stash_label_add
from urbanlens.dashboard.services.undo.service import (
    applying_undo,
    redo_latest,
    stack_state,
    stash_for_undo,
    undo_latest,
)


class UndoMutationStackTests(TestCase):
    """Linear undo/redo over stashed mutations, including redo-stack discard."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make("auth.User")
        self.profile = self.user.profile
        self.pin = baker.make_recipe("dashboard.pin", profile=self.profile, name="Mill")

    def test_moving_a_pin_is_undoable_and_redoable(self) -> None:
        origin_lat = float(self.pin.effective_latitude)
        origin_lng = float(self.pin.effective_longitude)
        move_pin_to_coordinates(self.pin, 41.7, -73.9)
        self.pin.refresh_from_db()
        self.assertAlmostEqual(float(self.pin.effective_latitude), 41.7, places=4)

        undo_latest(self.profile)
        self.pin.refresh_from_db()
        self.assertAlmostEqual(float(self.pin.effective_latitude), origin_lat, places=4)
        self.assertAlmostEqual(float(self.pin.effective_longitude), origin_lng, places=4)

        state = stack_state(self.profile)
        self.assertFalse(state["can_undo"])
        self.assertTrue(state["can_redo"])

        redo_latest(self.profile)
        self.pin.refresh_from_db()
        self.assertAlmostEqual(float(self.pin.effective_latitude), 41.7, places=4)

    def test_a_new_action_discards_the_redo_stack(self) -> None:
        move_pin_to_coordinates(self.pin, 10.0, 20.0)
        undo_latest(self.profile)
        self.assertTrue(stack_state(self.profile)["can_redo"])

        move_pin_to_coordinates(self.pin, 30.0, 40.0)

        state = stack_state(self.profile)
        self.assertTrue(state["can_undo"])
        self.assertFalse(state["can_redo"])
        self.assertEqual(UndoAction.objects.for_profile(self.profile).redoable().count(), 0)

    def test_stash_during_an_apply_is_a_no_op(self) -> None:
        with applying_undo():
            self.assertIsNone(stash_for_undo("pin", [self.pin], self.profile))
        self.assertFalse(UndoAction.objects.for_profile(self.profile).exists())

    def test_adding_and_removing_a_label_round_trips(self) -> None:
        label = ensure_label(profile=self.profile, name="Industrial", kind="tag")
        self.pin.labels.add(label)
        stash_label_add(self.profile, target="pin", target_id=self.pin.pk, label=label)

        undo_latest(self.profile)
        self.assertFalse(self.pin.labels.filter(pk=label.pk).exists())

        redo_latest(self.profile)
        self.assertTrue(self.pin.labels.filter(pk=label.pk).exists())

    def test_adding_an_alias_round_trips(self) -> None:
        alias = create_pin_alias(self.pin, name="Old Mill")
        self.assertTrue(PinAlias.objects.filter(pk=alias.pk).exists())

        undo_latest(self.profile)
        self.assertFalse(PinAlias.objects.filter(pin=self.pin, name="Old Mill").exists())

        redo_latest(self.profile)
        self.assertTrue(PinAlias.objects.filter(pin=self.pin, name="Old Mill").exists())
        undo_latest(self.profile)
        self.assertFalse(PinAlias.objects.filter(pin=self.pin, name="Old Mill").exists())

    def test_adding_a_photo_to_an_album_round_trips(self) -> None:
        image = baker.make_recipe("dashboard.image", pin=self.pin, profile=self.profile)
        album = Album.objects.create(name="Interior", profile=self.profile, parent_pin=self.pin)
        add_images_to_album(album, [image], self.profile)
        from urbanlens.dashboard.services.undo.mutations import stash_album_add

        stash_album_add(self.profile, album, [image.pk])
        self.assertTrue(AlbumItem.objects.filter(album=album, image=image).exists())

        undo_latest(self.profile)
        self.assertFalse(AlbumItem.objects.filter(album=album, image=image).exists())

        redo_latest(self.profile)
        self.assertTrue(AlbumItem.objects.filter(album=album, image=image).exists())

    def test_repositioning_a_photo_on_the_map_round_trips(self) -> None:
        image = baker.make_recipe(
            "dashboard.image",
            pin=self.pin,
            profile=self.profile,
            latitude="41.0",
            longitude="-73.0",
        )
        apply_image_map_update(image, json.dumps({"latitude": 42.1, "longitude": -74.2}).encode())
        image.refresh_from_db()
        self.assertAlmostEqual(float(image.latitude), 42.1, places=3)

        undo_latest(self.profile)
        image.refresh_from_db()
        self.assertAlmostEqual(float(image.latitude), 41.0, places=3)

        redo_latest(self.profile)
        image.refresh_from_db()
        self.assertAlmostEqual(float(image.latitude), 42.1, places=3)


class UndoStackViewTests(TestCase):
    """GET /undo/stack/ and POST /undo/undo/ / /undo/redo/."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make("auth.User")
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.pin = baker.make_recipe("dashboard.pin", profile=self.profile, name="Mill")

    def test_empty_stack_hides_both_buttons(self) -> None:
        response = self.client.get(reverse("undo.stack"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"ok": True, "can_undo": False, "can_redo": False, "undo_label": None, "redo_label": None, "undo_uuid": None, "redo_uuid": None},
        )

    def test_undo_and_redo_endpoints_walk_the_stack(self) -> None:
        origin_lat = float(self.pin.effective_latitude)
        move_pin_to_coordinates(self.pin, 12.0, 34.0)
        self.pin.refresh_from_db()
        moved_lat = float(self.pin.effective_latitude)

        response = self.client.post(reverse("undo.perform"))
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["can_undo"])
        self.assertTrue(payload["can_redo"])

        self.pin.refresh_from_db()
        self.assertAlmostEqual(float(self.pin.effective_latitude), origin_lat, places=4)

        response = self.client.post(reverse("undo.redo"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["can_undo"])
        self.assertFalse(response.json()["can_redo"])
        self.pin.refresh_from_db()
        self.assertAlmostEqual(float(self.pin.effective_latitude), moved_lat, places=4)

    def test_undo_on_an_empty_stack_is_a_conflict(self) -> None:
        response = self.client.post(reverse("undo.perform"))
        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json()["ok"])
