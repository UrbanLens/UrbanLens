"""A root pin and its child pin may share a Location; only two root pins of one profile may not."""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin, SwapLocationConflictError


class RootChildSharedLocationTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.location = baker.make(Location, latitude=43.0, longitude=-74.0)
        self.root = baker.make(Pin, profile=self.profile, location=self.location, name="Root")
        self.root.slug = self.root.ensure_slug()
        self.child = baker.make(Pin, profile=self.profile, location=self.location, parent_pin=self.root, name="Child")
        self.child.slug = self.child.ensure_slug()

    def test_swap_with_a_parent_at_the_same_location_succeeds(self) -> None:
        old_parent = self.child.swap_with_parent()

        self.child.refresh_from_db()
        old_parent.refresh_from_db()
        self.assertIsNone(self.child.parent_pin_id)
        self.assertEqual(old_parent.parent_pin_id, self.child.pk)

    def test_swap_is_refused_when_another_root_holds_the_location(self) -> None:
        other_root = baker.make(
            Pin, profile=self.profile, location=baker.make(Location, latitude=43.5, longitude=-74.5)
        )
        sub = baker.make(Pin, profile=self.profile, location=self.location, parent_pin=other_root)

        with self.assertRaises(SwapLocationConflictError):
            sub.swap_with_parent()

    def test_swap_endpoint_reports_a_structured_conflict(self) -> None:
        other_root = baker.make(
            Pin, profile=self.profile, location=baker.make(Location, latitude=43.5, longitude=-74.5)
        )
        sub = baker.make(Pin, profile=self.profile, location=self.location, parent_pin=other_root)
        sub.slug = sub.ensure_slug()

        response = self.client.post(reverse("pin.swap_parent", kwargs={"pin_slug": sub.slug}))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "root_location_conflict")
        self.assertIn("top-level pin", response.json()["error"])

    def test_detach_is_refused_with_a_clear_message(self) -> None:
        response = self.client.post(reverse("pin.detach_parent", kwargs={"pin_slug": self.child.slug}))

        self.assertEqual(response.status_code, 400)
        self.assertIn("top-level pin at this exact location", response.content.decode())
        self.child.refresh_from_db()
        self.assertEqual(self.child.parent_pin_id, self.root.pk)

    def test_promote_leaves_a_child_at_the_roots_location_and_says_so(self) -> None:
        elsewhere = baker.make(
            Pin,
            profile=self.profile,
            location=baker.make(Location, latitude=43.1, longitude=-74.1),
            parent_pin=self.root,
        )

        response = self.client.post(reverse("pin.promote_children", kwargs={"pin_slug": self.root.slug}))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["promoted"], 1)
        self.assertEqual(response.json()["skipped"], 1)
        self.assertIn("message", response.json())
        elsewhere.refresh_from_db()
        self.child.refresh_from_db()
        self.assertIsNone(elsewhere.parent_pin_id)
        self.assertEqual(self.child.parent_pin_id, self.root.pk)

    def test_get_nearby_or_create_prefers_the_root_over_an_older_child(self) -> None:
        # Make the child strictly older than the root, so only root-first ranking can pick the root.
        Pin.objects.filter(pk=self.child.pk).update(created=self.root.created - timedelta(days=1))
        pin, created = Pin.objects.get_nearby_or_create(43.0, -74.0, profile=self.profile)

        self.assertFalse(created)
        self.assertEqual(pin.pk, self.root.pk)

    def test_preferring_root_orders_root_then_oldest(self) -> None:
        second_child = baker.make(Pin, profile=self.profile, location=self.location, parent_pin=self.root)

        ordered = list(Pin.objects.filter(profile=self.profile, location=self.location).preferring_root())

        self.assertEqual([pin.pk for pin in ordered], [self.root.pk, self.child.pk, second_child.pk])

    def test_swap_to_top_level_refits_the_new_parents_boundary(self) -> None:
        from unittest import mock

        with mock.patch("urbanlens.dashboard.services.geo.child_pin_boundaries.refit_child_pin_boundary") as refit:
            self.child.swap_with_parent()

        refitted = {call.args[0] for call in refit.call_args_list}
        self.assertIn(self.child.pk, refitted)
        self.assertIn(self.root.pk, refitted)

    def test_swap_integrity_error_from_a_concurrent_root_is_a_conflict(self) -> None:
        from unittest import mock

        from django.db import IntegrityError

        other_root = baker.make(
            Pin, profile=self.profile, location=baker.make(Location, latitude=43.5, longitude=-74.5)
        )
        free_location = baker.make(Location, latitude=45.0, longitude=-76.0)
        racer = baker.make(Pin, profile=self.profile, location=free_location)
        sub = baker.make(Pin, profile=self.profile, location=free_location, parent_pin=other_root)
        real_save = Pin.save

        def racing_save(pin, *args, **kwargs):
            if pin.pk == sub.pk:
                raise IntegrityError("db_pin_unique_location_per_profile")
            return real_save(pin, *args, **kwargs)

        # The pre-check sees no root at sub's point; the recheck after the IntegrityError sees the racing root.
        # A write inside racing_save would be rolled back with the atomic block, so the lookup is patched instead.
        root_lookups = [Pin.objects.none(), Pin.objects.filter(pk=racer.pk)]
        with (
            mock.patch.object(Pin, "save", racing_save),
            mock.patch.object(Pin.objects, "root_pins", side_effect=root_lookups) as root_pins,
            self.assertRaises(SwapLocationConflictError),
        ):
            sub.swap_with_parent()

        self.assertEqual(root_pins.call_count, 2)

    def test_deleting_a_parent_keeps_two_children_at_its_point(self) -> None:
        from urbanlens.dashboard.services.pins.pin_edit import delete_pin

        second = baker.make(Pin, profile=self.profile, location=self.location, parent_pin=self.root)

        delete_pin(self.root, children_mode="keep")

        roots = Pin.objects.root_pins().filter(profile=self.profile, location=self.location)
        self.assertEqual(roots.count(), 1)
        remaining = Pin.objects.filter(pk__in=[self.child.pk, second.pk])
        self.assertEqual(remaining.count(), 2)
        survivor = roots.get()
        self.assertEqual({p.parent_pin_id for p in remaining if p.pk != survivor.pk}, {survivor.pk})

    def test_import_match_prefers_the_root_when_a_child_shares_its_location(self) -> None:
        pin, created = Pin.objects.get_nearby_or_create(43.0, -74.0, self.profile)

        self.assertFalse(created)
        self.assertEqual(pin.pk, self.root.pk)
