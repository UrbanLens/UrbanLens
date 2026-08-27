"""Tests for the external API's pin bulk-action endpoints: delete, merge, edit.

The behavior most worth pinning down: a pin in ``uuids``/``source_uuids`` that
isn't the caller's own is silently dropped, not refused (`pins/deleted/` +
`pins/` are the sync surface an offline client trusts, and a queued batch
replay shouldn't fail wholesale over one pin gone on another device) - but an
unresolvable label or parent uuid is a 400, since the client asked for
something specific and impossible.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.labels import ensure_label
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.auto_removals.model import AutoRemovalKind, PinAutoRemoval
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.reviews.model import Review
from urbanlens.dashboard.models.undo.model import UndoAction
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.services.pins.pin_creation import create_pin_for_profile

_BASE = "/dashboard/api/external/v1/pins/bulk/"


def _bearer(raw_key: str) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


class PinBulkTestCase(TestCase):
    """Shared fixture: one user with a write-scoped key and a couple of pins."""

    def setUp(self) -> None:
        baker.make(User)  # first user auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        _api_key, self.raw_key = generate_api_key(self.user, "Bulk client")
        ApiKey.objects.filter(user=self.user).update(scopes=[ApiKeyScope.PINS_READ.value, ApiKeyScope.PINS_WRITE.value])
        self.pin_a = create_pin_for_profile(self.profile, name="Pin A", latitude=42.5, longitude=-73.5).pin
        self.pin_b = create_pin_for_profile(self.profile, name="Pin B", latitude=43.5, longitude=-74.5).pin

    def _other_users_pin(self) -> Pin:
        other = baker.make(User)
        return create_pin_for_profile(Profile.objects.get(user=other), name="Not yours", latitude=1.0, longitude=1.0).pin

    def _post(self, path: str, payload: dict):
        return self.client.post(path, data=payload, content_type="application/json", **_bearer(self.raw_key))


class PinBulkDeleteTests(PinBulkTestCase):
    def test_deletes_every_named_pin_and_returns_an_undo_uuid(self) -> None:
        response = self._post(f"{_BASE}delete/", {"uuids": [str(self.pin_a.uuid), str(self.pin_b.uuid)]})
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["deleted"], 2)
        self.assertEqual(body["descendant_count"], 0)
        self.assertEqual(body["total_count"], 2)
        self.assertFalse(Pin.objects.filter(pk__in=[self.pin_a.pk, self.pin_b.pk]).exists())
        self.assertTrue(UndoAction.objects.filter(uuid=body["undo_uuid"], profile=self.profile, model_label="pin").exists())

    def test_deleting_a_parent_sweeps_its_children_and_counts_them(self) -> None:
        child = baker.make("dashboard.Pin", profile=self.profile, location=self.pin_a.location, parent_pin=self.pin_a, name="Entrance")
        response = self._post(f"{_BASE}delete/", {"uuids": [str(self.pin_a.uuid)]})
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["deleted"], 1)
        self.assertEqual(body["descendant_count"], 1)
        self.assertEqual(body["total_count"], 2)
        self.assertFalse(Pin.objects.filter(pk__in=[self.pin_a.pk, child.pk]).exists())

    def test_another_users_pin_is_silently_dropped_not_refused(self) -> None:
        theirs = self._other_users_pin()
        response = self._post(f"{_BASE}delete/", {"uuids": [str(self.pin_a.uuid), str(theirs.uuid)]})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["deleted"], 1)
        self.assertTrue(Pin.objects.filter(pk=theirs.pk).exists())

    def test_no_matching_pins_is_a_404(self) -> None:
        theirs = self._other_users_pin()
        response = self._post(f"{_BASE}delete/", {"uuids": [str(theirs.uuid)]})
        self.assertEqual(response.status_code, 404)

    def test_requires_pins_write_scope(self) -> None:
        ApiKey.objects.filter(user=self.user).update(scopes=[ApiKeyScope.PINS_READ.value])
        response = self._post(f"{_BASE}delete/", {"uuids": [str(self.pin_a.uuid)]})
        self.assertEqual(response.status_code, 403)

    def test_empty_uuids_is_a_400(self) -> None:
        self.assertEqual(self._post(f"{_BASE}delete/", {"uuids": []}).status_code, 400)


class PinBulkMergeTests(PinBulkTestCase):
    def test_merges_sources_into_the_target_as_detail_pins(self) -> None:
        response = self._post(f"{_BASE}merge/", {"target_uuid": str(self.pin_a.uuid), "source_uuids": [str(self.pin_b.uuid)]})
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["target"]["uuid"], str(self.pin_a.uuid))
        self.assertEqual(body["merged_uuids"], [str(self.pin_b.uuid)])
        self.assertEqual(body["skipped_uuids"], [])
        self.pin_b.refresh_from_db()
        self.assertEqual(self.pin_b.parent_pin_id, self.pin_a.pk)

    def test_promotes_a_child_target_to_top_level_first(self) -> None:
        pin_c = create_pin_for_profile(self.profile, name="Pin C", latitude=44.5, longitude=-75.5).pin
        Pin.objects.filter(pk=self.pin_b.pk).update(parent_pin=pin_c)
        response = self._post(f"{_BASE}merge/", {"target_uuid": str(self.pin_b.uuid), "source_uuids": [str(self.pin_a.uuid)]})
        self.assertEqual(response.status_code, 200, response.content)
        self.pin_b.refresh_from_db()
        self.assertIsNone(self.pin_b.parent_pin_id)

    def test_promotion_conflict_with_an_existing_top_level_pin_is_a_400(self) -> None:
        """pin_b becomes a child of pin_a at pin_a's own Location - promoting it collides with pin_a itself."""
        Pin.objects.filter(pk=self.pin_b.pk).update(parent_pin=self.pin_a, location_id=self.pin_a.location_id)
        pin_c = create_pin_for_profile(self.profile, name="Pin C", latitude=44.5, longitude=-75.5).pin
        response = self._post(f"{_BASE}merge/", {"target_uuid": str(self.pin_b.uuid), "source_uuids": [str(pin_c.uuid)]})
        self.assertEqual(response.status_code, 400)
        self.assertIn("already have a top-level pin", response.json()["error"])

    def test_source_already_a_child_of_the_target_merges_without_incident(self) -> None:
        # would_create_cycle can only be True when the target still has its own
        # parent - by the time sources are processed the target has always been
        # promoted to root, so this exercises the ordinary (non-cycle) path for
        # a source that happens to already be one of the target's children.
        child = baker.make("dashboard.Pin", profile=self.profile, location=self.pin_a.location, parent_pin=self.pin_a, name="Entrance")
        response = self._post(f"{_BASE}merge/", {"target_uuid": str(self.pin_a.uuid), "source_uuids": [str(child.uuid), str(self.pin_b.uuid)]})
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(set(body["merged_uuids"]), {str(child.uuid), str(self.pin_b.uuid)})
        self.assertEqual(body["skipped_uuids"], [])

    def test_unknown_target_is_a_404(self) -> None:
        theirs = self._other_users_pin()
        response = self._post(f"{_BASE}merge/", {"target_uuid": str(theirs.uuid), "source_uuids": [str(self.pin_a.uuid)]})
        self.assertEqual(response.status_code, 404)

    def test_no_valid_sources_is_a_400(self) -> None:
        theirs = self._other_users_pin()
        response = self._post(f"{_BASE}merge/", {"target_uuid": str(self.pin_a.uuid), "source_uuids": [str(theirs.uuid)]})
        self.assertEqual(response.status_code, 400)

    def test_promotion_does_not_persist_when_sources_are_all_invalid(self) -> None:
        """A rejected merge must not leave the target promoted to top-level."""
        pin_c = create_pin_for_profile(self.profile, name="Pin C", latitude=44.5, longitude=-75.5).pin
        Pin.objects.filter(pk=self.pin_b.pk).update(parent_pin=pin_c)
        theirs = self._other_users_pin()
        response = self._post(f"{_BASE}merge/", {"target_uuid": str(self.pin_b.uuid), "source_uuids": [str(theirs.uuid)]})
        self.assertEqual(response.status_code, 400)
        self.pin_b.refresh_from_db()
        self.assertEqual(self.pin_b.parent_pin_id, pin_c.pk)


class PinBulkEditTests(PinBulkTestCase):
    def test_sets_description_on_every_pin(self) -> None:
        response = self._post(f"{_BASE}edit/", {"uuids": [str(self.pin_a.uuid), str(self.pin_b.uuid)], "description": "Shared note"})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), {"count": 2, "reparented": 0})
        self.pin_a.refresh_from_db()
        self.pin_b.refresh_from_db()
        self.assertEqual(self.pin_a.description, "Shared note")
        self.assertEqual(self.pin_b.description, "Shared note")

    def test_explicit_null_description_clears_it(self) -> None:
        Pin.objects.filter(pk=self.pin_a.pk).update(description="Old note")
        response = self._post(f"{_BASE}edit/", {"uuids": [str(self.pin_a.uuid)], "description": None})
        self.assertEqual(response.status_code, 200, response.content)
        self.pin_a.refresh_from_db()
        self.assertIsNone(self.pin_a.description)

    def test_absent_description_leaves_it_untouched(self) -> None:
        Pin.objects.filter(pk=self.pin_a.pk).update(description="Keep me")
        response = self._post(f"{_BASE}edit/", {"uuids": [str(self.pin_a.uuid)], "rating": 5})
        self.assertEqual(response.status_code, 200, response.content)
        self.pin_a.refresh_from_db()
        self.assertEqual(self.pin_a.description, "Keep me")

    def test_sets_a_rating_via_review(self) -> None:
        response = self._post(f"{_BASE}edit/", {"uuids": [str(self.pin_a.uuid), str(self.pin_b.uuid)], "rating": 4})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(Review.objects.get(profile=self.profile, pin=self.pin_a).rating, 4)
        self.assertEqual(Review.objects.get(profile=self.profile, pin=self.pin_b).rating, 4)

    def test_null_rating_clears_existing_reviews(self) -> None:
        Review.objects.create(profile=self.profile, pin=self.pin_a, rating=3)
        response = self._post(f"{_BASE}edit/", {"uuids": [str(self.pin_a.uuid)], "rating": None})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(Review.objects.filter(profile=self.profile, pin=self.pin_a).exists())

    def test_adds_labels_to_every_pin(self) -> None:
        label = ensure_label(profile=self.profile, name="Rusty", kind="tag")
        response = self._post(f"{_BASE}edit/", {"uuids": [str(self.pin_a.uuid), str(self.pin_b.uuid)], "add_label_uuids": [str(label.uuid)]})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(self.pin_a.labels.filter(pk=label.pk).exists())
        self.assertTrue(self.pin_b.labels.filter(pk=label.pk).exists())

    def test_unresolvable_add_label_uuid_is_a_400(self) -> None:
        response = self._post(f"{_BASE}edit/", {"uuids": [str(self.pin_a.uuid)], "add_label_uuids": ["00000000-0000-0000-0000-000000000000"]})
        self.assertEqual(response.status_code, 400)

    def test_removing_a_label_writes_an_auto_removal_tombstone(self) -> None:
        label = ensure_label(profile=self.profile, name="Rusty", kind="tag")
        self.pin_a.labels.add(label)
        response = self._post(f"{_BASE}edit/", {"uuids": [str(self.pin_a.uuid)], "remove_label_uuids": [str(label.uuid)]})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(self.pin_a.labels.filter(pk=label.pk).exists())
        self.assertTrue(PinAutoRemoval.objects.filter(pin=self.pin_a, kind=AutoRemovalKind.LABEL, value=str(label.pk)).exists())

    def test_removing_a_label_absent_from_a_pin_is_a_silent_no_op(self) -> None:
        label = ensure_label(profile=self.profile, name="Rusty", kind="tag")
        # Not attached to pin_a - removal must succeed without incident.
        response = self._post(f"{_BASE}edit/", {"uuids": [str(self.pin_a.uuid)], "remove_label_uuids": [str(label.uuid)]})
        self.assertEqual(response.status_code, 200, response.content)

    def test_reparents_every_pin_under_the_named_target(self) -> None:
        parent = create_pin_for_profile(self.profile, name="Campus", latitude=50.0, longitude=50.0).pin
        response = self._post(f"{_BASE}edit/", {"uuids": [str(self.pin_a.uuid), str(self.pin_b.uuid)], "parent_uuid": str(parent.uuid)})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["reparented"], 2)
        self.pin_a.refresh_from_db()
        self.pin_b.refresh_from_db()
        self.assertEqual(self.pin_a.parent_pin_id, parent.pk)
        self.assertEqual(self.pin_b.parent_pin_id, parent.pk)

    def test_null_parent_uuid_detaches_every_pin(self) -> None:
        parent = create_pin_for_profile(self.profile, name="Campus", latitude=50.0, longitude=50.0).pin
        Pin.objects.filter(pk=self.pin_a.pk).update(parent_pin=parent)
        response = self._post(f"{_BASE}edit/", {"uuids": [str(self.pin_a.uuid)], "parent_uuid": None})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["reparented"], 1)
        self.pin_a.refresh_from_db()
        self.assertIsNone(self.pin_a.parent_pin_id)

    def test_reparenting_under_a_descendant_skips_that_pin_without_failing_the_batch(self) -> None:
        child = baker.make("dashboard.Pin", profile=self.profile, location=self.pin_a.location, parent_pin=self.pin_a, name="Entrance")
        response = self._post(f"{_BASE}edit/", {"uuids": [str(self.pin_a.uuid), str(self.pin_b.uuid)], "parent_uuid": str(child.uuid)})
        self.assertEqual(response.status_code, 200, response.content)
        # pin_a would become its own descendant via child - skipped; pin_b is fine.
        self.assertEqual(response.json()["reparented"], 1)
        self.pin_b.refresh_from_db()
        self.assertEqual(self.pin_b.parent_pin_id, child.pk)

    def test_unknown_parent_uuid_is_a_400(self) -> None:
        response = self._post(f"{_BASE}edit/", {"uuids": [str(self.pin_a.uuid)], "parent_uuid": "00000000-0000-0000-0000-000000000000"})
        self.assertEqual(response.status_code, 400)

    def test_detaching_a_pin_whose_location_conflicts_with_an_existing_root_is_skipped(self) -> None:
        """Detaching must not raise IntegrityError when the pin's own Location already has an unrelated top-level pin."""
        child = baker.make("dashboard.Pin", profile=self.profile, location=self.pin_a.location, parent_pin=self.pin_b, name="Shares pin_a's spot")
        response = self._post(f"{_BASE}edit/", {"uuids": [str(child.uuid)], "parent_uuid": None})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["reparented"], 0)
        child.refresh_from_db()
        self.assertEqual(child.parent_pin_id, self.pin_b.pk)

    def test_invalid_add_label_uuid_rolls_back_earlier_edits_in_the_same_batch(self) -> None:
        response = self._post(
            f"{_BASE}edit/",
            {
                "uuids": [str(self.pin_a.uuid)],
                "description": "Should not stick",
                "add_label_uuids": ["00000000-0000-0000-0000-000000000000"],
            },
        )
        self.assertEqual(response.status_code, 400)
        self.pin_a.refresh_from_db()
        self.assertNotEqual(self.pin_a.description, "Should not stick")

    def test_another_users_pin_is_silently_dropped_from_the_batch(self) -> None:
        theirs = self._other_users_pin()
        response = self._post(f"{_BASE}edit/", {"uuids": [str(self.pin_a.uuid), str(theirs.uuid)], "description": "Shared"})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["count"], 1)
        theirs.refresh_from_db()
        self.assertNotEqual(theirs.description, "Shared")

    def test_no_matching_pins_is_a_404(self) -> None:
        theirs = self._other_users_pin()
        response = self._post(f"{_BASE}edit/", {"uuids": [str(theirs.uuid)], "description": "nope"})
        self.assertEqual(response.status_code, 404)

    def test_requires_pins_write_scope(self) -> None:
        ApiKey.objects.filter(user=self.user).update(scopes=[ApiKeyScope.PINS_READ.value])
        response = self._post(f"{_BASE}edit/", {"uuids": [str(self.pin_a.uuid)], "description": "nope"})
        self.assertEqual(response.status_code, 403)
