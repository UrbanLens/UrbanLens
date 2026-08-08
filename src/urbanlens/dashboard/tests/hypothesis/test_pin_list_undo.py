"""Deleting a pin list is restorable from Undo History, like every comparable delete.

Pins, wikis, trips, safety check-ins and saved filters already stash before deleting;
lists were the gap - and a list is exactly the kind of thing undo exists for, since
deleting one destroys hand-built curation (which pins, in what order) while the pins
themselves survive.
"""

from __future__ import annotations

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_list.model import PinList, PinListItem
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.undo.model import UndoAction
from urbanlens.dashboard.services.undo.service import UndoExpiredError, restore_undo_action, stash_for_undo


class PinListUndoTests(TestCase):
    """The pin_list handler round-trips a list and refuses what it must."""

    def setUp(self):
        super().setUp()
        self.profile: Profile = baker.make("auth.User").profile
        self.pins = []
        for index in range(3):
            location = Location.objects.create(latitude=49.0 + index, longitude=-66.0 - index)
            self.pins.append(Pin.objects.create(profile=self.profile, location=location, name=f"Stop {index}"))

        self.pin_list = PinList.objects.create(profile=self.profile, name="Weekend route", description="Three mills")
        for order, pin in enumerate(self.pins):
            PinListItem.objects.create(pin_list=self.pin_list, pin=pin, order=order)

    def _delete_with_undo(self) -> UndoAction:
        undo_action = stash_for_undo("pin_list", [self.pin_list], self.profile)
        self.pin_list.delete()
        return undo_action

    def test_an_ordinary_undo_restores_the_list_and_its_members(self):
        undo_action = self._delete_with_undo()

        restored = restore_undo_action(undo_action)

        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0].name, "Weekend route")
        self.assertEqual(restored[0].description, "Three mills")
        members = list(restored[0].items.order_by("order").values_list("pin_id", flat=True))
        self.assertEqual(members, [pin.pk for pin in self.pins])

    def test_the_restored_list_gets_a_fresh_slug(self):
        old_slug = self.pin_list.slug
        undo_action = self._delete_with_undo()
        # The old slug is taken again before the restore.
        PinList.objects.create(profile=self.profile, name="Weekend route replacement", slug=old_slug)

        restored = restore_undo_action(undo_action)

        self.assertNotEqual(restored[0].slug, old_slug)

    def test_a_member_pin_deleted_since_is_skipped_not_fatal(self):
        undo_action = self._delete_with_undo()
        self.pins[1].delete()

        restored = restore_undo_action(undo_action)

        members = list(restored[0].items.order_by("order").values_list("pin_id", flat=True))
        self.assertEqual(members, [self.pins[0].pk, self.pins[2].pk])

    def test_undo_is_refused_when_the_name_has_been_reused(self):
        undo_action = self._delete_with_undo()
        PinList.objects.create(profile=self.profile, name="Weekend route")

        with self.assertRaises(UndoExpiredError):
            restore_undo_action(undo_action)

    def test_another_profiles_list_with_the_same_name_does_not_block(self):
        undo_action = self._delete_with_undo()
        other: Profile = baker.make("auth.User").profile
        PinList.objects.create(profile=other, name="Weekend route")

        self.assertEqual(len(restore_undo_action(undo_action)), 1)

    def test_a_smart_lists_rules_survive_the_round_trip(self):
        self.pin_list.is_smart = True
        self.pin_list.smart_filter = {"labels": ["abandoned"]}
        self.pin_list.save(update_fields=["is_smart", "smart_filter", "updated"])
        undo_action = self._delete_with_undo()

        restored = restore_undo_action(undo_action)

        self.assertTrue(restored[0].is_smart)
        self.assertEqual(restored[0].smart_filter, {"labels": ["abandoned"]})

    def test_a_smart_boundary_survives_as_geometry(self):
        from django.contrib.gis.geos import MultiPolygon, Polygon

        boundary = MultiPolygon(Polygon(((0, 0), (0, 1), (1, 1), (0, 0))))
        self.pin_list.smart_boundary = boundary
        self.pin_list.save(update_fields=["smart_boundary", "updated"])
        undo_action = self._delete_with_undo()

        restored = restore_undo_action(undo_action)

        self.assertIsNotNone(restored[0].smart_boundary)
        self.assertEqual(restored[0].smart_boundary.srid, 4326)

    def test_a_dead_saved_filter_link_is_dropped_not_fatal(self):
        from urbanlens.dashboard.models.saved_filter.model import SavedFilter

        source = SavedFilter.objects.create(profile=self.profile, name="Mills", criteria={})
        self.pin_list.source_saved_filter = source
        self.pin_list.save(update_fields=["source_saved_filter", "updated"])
        undo_action = self._delete_with_undo()
        source.delete()

        restored = restore_undo_action(undo_action)

        self.assertIsNone(restored[0].source_saved_filter_id)

    def test_the_delete_view_stashes(self):
        self.client.force_login(self.profile.user)

        response = self.client.post(f"/dashboard/lists/{self.pin_list.slug}/delete/")

        self.assertEqual(response.status_code, 302)
        self.assertFalse(PinList.objects.filter(pk=self.pin_list.pk).exists())
        action = UndoAction.objects.for_profile(self.profile).filter(model_label="pin_list").first()
        self.assertIsNotNone(action, "the web delete endpoint did not stash for undo")
        self.assertIn("Weekend route", action.object_repr)
