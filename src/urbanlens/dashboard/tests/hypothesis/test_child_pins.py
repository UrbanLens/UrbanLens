"""Tests for child (sub) pin functionality: merge property retention, the main
map's Child Pins layer, jump-to-pin search coverage, detaching a child pin, the
pin page's "show sub pin details" toggle endpoints, share bundles, and the
Visited-label propagation to ancestors."""

from __future__ import annotations

import json

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus
from urbanlens.dashboard.models.labels.meta import KIND_STATUS, KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_share import PinShare, PinShareStatus
from urbanlens.dashboard.models.visits.model import PinVisit
from urbanlens.dashboard.services.map_pins.autocomplete import search_local

_coord_counter = 0


def _make_pin(profile, **kwargs) -> Pin:
    """Create a pin with a real coordinate-bearing Location.

    Locations are unique per (latitude, longitude), so each generated pin
    gets its own distinct coordinates.
    """
    global _coord_counter
    location = kwargs.pop("location", None)
    if location is None:
        _coord_counter += 1
        location = baker.make(Location, latitude=42.0 + _coord_counter * 0.001, longitude=-73.0 - _coord_counter * 0.001)
    return baker.make(Pin, profile=profile, location=location, **kwargs)


class MergeRetainsPropertiesTests(TestCase):
    """Merging pins must not alter any of the merged pins' own properties."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.target = _make_pin(self.profile, name="Target")
        self.label = baker.make(Label, kind=KIND_TAG, profile=self.profile, name="rooftop")
        self.source = _make_pin(
            self.profile,
            name="Old Mill",
            icon="factory",
            color="#F44336",
            description="my private notes",
            priority=4,
            danger=3,
        )
        self.source.labels.add(self.label)

    def test_merged_pin_keeps_name_icon_labels_and_notes(self) -> None:
        response = self.client.post(
            reverse("pin.bulk_merge"),
            data=json.dumps({"target_uuid": str(self.target.uuid), "source_uuids": [str(self.source.uuid)]}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.source.refresh_from_db()
        self.assertEqual(self.source.parent_pin_id, self.target.pk)
        self.assertEqual(self.source.name, "Old Mill")
        self.assertEqual(self.source.icon, "factory")
        self.assertEqual(self.source.color, "#F44336")
        self.assertEqual(self.source.description, "my private notes")
        self.assertEqual(self.source.priority, 4)
        self.assertEqual(self.source.danger, 3)
        self.assertIn(self.label, self.source.labels.all())


class MapChildPinsJsonTests(TestCase):
    """GET /map/pins/children/ returns the profile's child pins at every depth."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.root = _make_pin(self.profile, name="Campus")
        self.child = _make_pin(self.profile, name="Boiler House", parent_pin=self.root)
        self.grandchild = _make_pin(self.profile, name="Basement Door", parent_pin=self.child)

    def _get(self, params: dict | None = None):
        return self.client.get(reverse("map.pins.children"), params or {})

    def test_returns_all_descendant_depths(self) -> None:
        names = {p["name"] for p in self._get().json()["pins"]}
        self.assertEqual(names, {"Boiler House", "Basement Door"})

    def test_excludes_root_pins(self) -> None:
        uuids = {p["uuid"] for p in self._get().json()["pins"]}
        self.assertNotIn(str(self.root.uuid), uuids)

    def test_includes_parent_link_fields(self) -> None:
        by_name = {p["name"]: p for p in self._get().json()["pins"]}
        self.assertEqual(by_name["Boiler House"]["parent_name"], "Campus")
        self.assertIn("/dashboard/map/pin/", by_name["Boiler House"]["parent_url"])
        self.assertIn("/dashboard/map/pin/", by_name["Boiler House"]["url"])

    def test_applies_filter_criteria(self) -> None:
        response = self._get({"name": "Basement"})
        names = {p["name"] for p in response.json()["pins"]}
        self.assertEqual(names, {"Basement Door"})

    def test_excludes_other_users_child_pins(self) -> None:
        other = baker.make(User)
        other_root = _make_pin(other.profile)
        _make_pin(other.profile, name="Not Mine", parent_pin=other_root)
        names = {p["name"] for p in self._get().json()["pins"]}
        self.assertNotIn("Not Mine", names)


class MapSearchExcludesChildPinsTests(TestCase):
    """POST /map/search/ (the filter-formula search path) must not surface child pins as if they were root pins.

    Unlike every other map-data query, this path built its queryset without
    ``.root_pins()`` before ``get_map_data()`` was called with an explicit
    (non-None) query - which only applies that filter itself when no query is
    given. Left unfixed, a search would show a merged/detail pin as a normal
    top-level marker, the opposite of the "merged pins disappear" symptom.
    """

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.root = _make_pin(self.profile, name="Campus")
        self.child = _make_pin(self.profile, name="Boiler House", parent_pin=self.root)

    def test_search_excludes_child_pins(self) -> None:
        response = self.client.post(reverse("map.search"), {})
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn(str(self.root.uuid), body)
        self.assertNotIn(str(self.child.uuid), body)


class SearchLocalChildPinTests(TestCase):
    """Jump-to-pin autocomplete must find child pins and mark them as such."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.root = _make_pin(self.profile, name="Asylum Grounds")
        self.child = _make_pin(self.profile, name="Morgue Wing", parent_pin=self.root)

    def test_child_pin_is_included_and_flagged(self) -> None:
        results = search_local("Morgue", self.profile)
        match = next((r for r in results if r.title == "Morgue Wing"), None)
        self.assertIsNotNone(match)
        self.assertTrue(match.is_child)
        self.assertIn("Asylum Grounds", match.subtitle)

    def test_root_pin_is_not_flagged_as_child(self) -> None:
        results = search_local("Asylum", self.profile)
        match = next((r for r in results if r.title == "Asylum Grounds"), None)
        self.assertIsNotNone(match)
        self.assertFalse(match.is_child)


class PinDetachChildViewTests(TestCase):
    """POST /map/pin/<slug>/detach-parent/ promotes a child pin to top level."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.root = _make_pin(self.profile, name="Parent")
        self.child = _make_pin(self.profile, name="Child", parent_pin=self.root)
        self.child.slug = self.child.ensure_slug()

    def _detach(self, pin: Pin):
        return self.client.post(reverse("pin.detach_parent", kwargs={"pin_slug": pin.slug or str(pin.uuid)}))

    def test_detaches_child_to_top_level(self) -> None:
        response = self._detach(self.child)
        self.assertEqual(response.status_code, 200)
        self.child.refresh_from_db()
        self.assertIsNone(self.child.parent_pin_id)

    def test_rejects_root_pin(self) -> None:
        self.root.slug = self.root.ensure_slug()
        response = self._detach(self.root)
        self.assertEqual(response.status_code, 400)

    def test_rejects_when_root_pin_already_exists_at_same_location(self) -> None:
        conflict_child = _make_pin(self.profile, parent_pin=self.root, location=self.root.location)
        conflict_child.slug = conflict_child.ensure_slug()
        response = self._detach(conflict_child)
        self.assertEqual(response.status_code, 400)
        conflict_child.refresh_from_db()
        self.assertEqual(conflict_child.parent_pin_id, self.root.pk)

    def test_rejects_other_users_pin(self) -> None:
        other = baker.make(User)
        self.client.force_login(other)
        response = self._detach(self.child)
        self.assertEqual(response.status_code, 404)
        self.child.refresh_from_db()
        self.assertEqual(self.child.parent_pin_id, self.root.pk)


class PinPromoteChildrenViewTests(TestCase):
    """POST /map/pin/<slug>/promote-children/ moves a pin's direct children up one level."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.grandparent = _make_pin(self.profile, name="Grandparent")
        self.grandparent.slug = self.grandparent.ensure_slug()
        self.parent = _make_pin(self.profile, name="Parent", parent_pin=self.grandparent)
        self.parent.slug = self.parent.ensure_slug()
        self.child_a = _make_pin(self.profile, name="Child A", parent_pin=self.parent)
        self.child_b = _make_pin(self.profile, name="Child B", parent_pin=self.parent)
        self.grandchild = _make_pin(self.profile, name="Grandchild", parent_pin=self.child_a)

    def _promote(self, pin: Pin):
        return self.client.post(reverse("pin.promote_children", kwargs={"pin_slug": pin.slug or str(pin.uuid)}))

    def test_children_move_to_pins_own_parent(self) -> None:
        response = self._promote(self.parent)
        self.assertEqual(response.status_code, 200)
        self.child_a.refresh_from_db()
        self.child_b.refresh_from_db()
        self.assertEqual(self.child_a.parent_pin_id, self.grandparent.pk)
        self.assertEqual(self.child_b.parent_pin_id, self.grandparent.pk)

    def test_children_become_top_level_when_pin_has_no_parent(self) -> None:
        root = _make_pin(self.profile, name="Root")
        root.slug = root.ensure_slug()
        child = _make_pin(self.profile, name="Root's Child", parent_pin=root)
        response = self._promote(root)
        self.assertEqual(response.status_code, 200)
        child.refresh_from_db()
        self.assertIsNone(child.parent_pin_id)

    def test_the_pin_itself_is_unaffected(self) -> None:
        self._promote(self.parent)
        self.parent.refresh_from_db()
        self.assertEqual(self.parent.parent_pin_id, self.grandparent.pk)

    def test_grandchildren_stay_nested_under_their_promoted_parent(self) -> None:
        self._promote(self.parent)
        self.grandchild.refresh_from_db()
        self.assertEqual(self.grandchild.parent_pin_id, self.child_a.pk)

    def test_returns_promoted_count(self) -> None:
        response = self._promote(self.parent)
        self.assertEqual(response.json()["promoted"], 2)

    def test_no_children_returns_400(self) -> None:
        response = self._promote(self.child_b)
        self.assertEqual(response.status_code, 400)

    def test_rejects_other_users_pin(self) -> None:
        other = baker.make(User)
        self.client.force_login(other)
        response = self._promote(self.parent)
        self.assertEqual(response.status_code, 404)
        self.child_a.refresh_from_db()
        self.assertEqual(self.child_a.parent_pin_id, self.parent.pk)

    def test_child_sharing_a_root_pins_own_location_is_left_in_place(self) -> None:
        """A root pin being promoted-from isn't deleted, so it keeps occupying its
        own root slot forever - a child at that same Location can't become root
        (there's no parent to move it under instead) and must stay put."""
        root = _make_pin(self.profile, name="Root")
        root.slug = root.ensure_slug()
        same_loc_child = _make_pin(self.profile, name="Same Spot", parent_pin=root, location=root.location)
        self._promote(root)
        same_loc_child.refresh_from_db()
        self.assertEqual(same_loc_child.parent_pin_id, root.pk)

    def test_child_sharing_a_non_root_pins_location_is_still_promoted(self) -> None:
        """When the pin being promoted-from itself has a parent, a child sharing
        its Location can still move to that parent - only *root* pins are
        constrained by Location, and the child isn't becoming one here."""
        same_loc_child = _make_pin(self.profile, name="Same Spot", parent_pin=self.parent, location=self.parent.location)
        self._promote(self.parent)
        same_loc_child.refresh_from_db()
        self.assertEqual(same_loc_child.parent_pin_id, self.grandparent.pk)

    def test_promoted_child_nests_under_existing_root_at_same_location(self) -> None:
        root = _make_pin(self.profile, name="Standalone Root")
        root.slug = root.ensure_slug()
        colliding_child = _make_pin(self.profile, name="Colliding Child", parent_pin=root, location=self.grandparent.location)
        self._promote(root)
        colliding_child.refresh_from_db()
        self.assertEqual(colliding_child.parent_pin_id, self.grandparent.pk)


class PinSwapWithParentModelTests(TestCase):
    """Pin.swap_with_parent() - the model-level hierarchy swap."""

    def setUp(self) -> None:
        self.profile = baker.make(User).profile
        self.grandparent = _make_pin(self.profile, name="Grandparent")
        self.parent = _make_pin(self.profile, name="Parent", parent_pin=self.grandparent)
        self.child = _make_pin(self.profile, name="Child", parent_pin=self.parent)
        self.sibling = _make_pin(self.profile, name="Sibling", parent_pin=self.parent)

    def test_child_becomes_parent_of_former_parent(self) -> None:
        old_parent = self.child.swap_with_parent()
        self.assertEqual(old_parent.pk, self.parent.pk)
        old_parent.refresh_from_db()
        self.child.refresh_from_db()
        self.assertEqual(old_parent.parent_pin_id, self.child.pk)

    def test_child_takes_over_grandparent_slot(self) -> None:
        self.child.swap_with_parent()
        self.child.refresh_from_db()
        self.assertEqual(self.child.parent_pin_id, self.grandparent.pk)

    def test_child_becomes_top_level_when_parent_had_no_parent(self) -> None:
        root = _make_pin(self.profile, name="Root")
        sub = _make_pin(self.profile, name="Sub", parent_pin=root)
        sub.swap_with_parent()
        sub.refresh_from_db()
        root.refresh_from_db()
        self.assertIsNone(sub.parent_pin_id)
        self.assertEqual(root.parent_pin_id, sub.pk)

    def test_siblings_of_the_promoted_child_are_unaffected(self) -> None:
        self.child.swap_with_parent()
        self.sibling.refresh_from_db()
        self.assertEqual(self.sibling.parent_pin_id, self.parent.pk)

    def test_raises_when_pin_has_no_parent(self) -> None:
        with self.assertRaises(ValueError):
            self.grandparent.swap_with_parent()

    def test_raises_on_location_conflict_at_grandparent_slot(self) -> None:
        """Swapping a pin one level under a root pin would leave the old
        parent as a second root pin at a Location that already has one."""
        root = _make_pin(self.profile, name="Root")
        conflicting_root = _make_pin(self.profile, name="Conflicting Root")
        sub = _make_pin(self.profile, name="Sub", parent_pin=conflicting_root, location=root.location)
        with self.assertRaises(ValueError):
            sub.swap_with_parent()
        sub.refresh_from_db()
        self.assertEqual(sub.parent_pin_id, conflicting_root.pk)

    def test_succeeds_despite_unrelated_root_conflict_at_own_location_when_grandparent_exists(self) -> None:
        """A conflicting root pin at this pin's own Location must not block the swap
        when a grandparent exists - this pin never actually needs to become a root
        pin in that case, so the one-root-per-Location constraint doesn't apply."""
        other_root = _make_pin(self.profile, name="Other Root", location=self.child.location)
        self.child.swap_with_parent()
        self.child.refresh_from_db()
        self.parent.refresh_from_db()
        other_root.refresh_from_db()
        self.assertEqual(self.child.parent_pin_id, self.grandparent.pk)
        self.assertEqual(self.parent.parent_pin_id, self.child.pk)
        self.assertIsNone(other_root.parent_pin_id)

    def test_no_cycle_after_swap(self) -> None:
        """The former parent's ancestor chain must never loop back to itself."""
        self.child.swap_with_parent()
        self.child.refresh_from_db()
        self.parent.refresh_from_db()
        chain = self.parent.ancestor_chain()
        self.assertEqual([p.pk for p in chain], [self.child.pk, self.grandparent.pk])


class PinSwapParentViewTests(TestCase):
    """POST /map/pin/<slug>/swap-parent/ - the child-pin popup's promote-to-parent action."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.parent = _make_pin(self.profile, name="Parent")
        self.parent.slug = self.parent.ensure_slug()
        self.child = _make_pin(self.profile, name="Child", parent_pin=self.parent)
        self.child.slug = self.child.ensure_slug()

    def _swap(self, pin: Pin):
        return self.client.post(reverse("pin.swap_parent", kwargs={"pin_slug": pin.slug or str(pin.uuid)}))

    def test_swap_succeeds_and_reassigns_parent_pin(self) -> None:
        response = self._swap(self.child)
        self.assertEqual(response.status_code, 200)
        self.parent.refresh_from_db()
        self.assertEqual(self.parent.parent_pin_id, self.child.pk)

    def test_response_reports_both_slugs(self) -> None:
        response = self._swap(self.child)
        data = response.json()
        self.assertEqual(data["new_parent_slug"], self.child.slug)
        self.assertEqual(data["new_child_slug"], self.parent.slug)

    def test_rejects_root_pin_with_no_parent(self) -> None:
        response = self._swap(self.parent)
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())

    def test_rejects_other_users_pin(self) -> None:
        other = baker.make(User)
        self.client.force_login(other)
        response = self._swap(self.child)
        self.assertEqual(response.status_code, 404)
        self.parent.refresh_from_db()
        self.assertIsNone(self.parent.parent_pin_id)

    def test_location_conflict_returns_400_with_message(self) -> None:
        root = _make_pin(self.profile, name="Root")
        root.slug = root.ensure_slug()
        conflicting_root = _make_pin(self.profile, name="Conflicting Root")
        sub = _make_pin(self.profile, name="Sub", parent_pin=conflicting_root, location=root.location)
        sub.slug = sub.ensure_slug()
        response = self._swap(sub)
        self.assertEqual(response.status_code, 400)
        self.assertIn("location", response.json()["error"].lower())


class DetailPinJsonChildrenTests(TestCase):
    """?children=1 expands the pin page's detail-pin JSON to the full subtree."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.root = _make_pin(self.profile, name="Root")
        self.root.slug = self.root.ensure_slug()
        self.child = _make_pin(self.profile, name="Child", parent_pin=self.root)
        self.grandchild = _make_pin(self.profile, name="Grandchild", parent_pin=self.child)

    def test_default_returns_direct_children_only(self) -> None:
        response = self.client.get(reverse("pin.detail_pins.json", kwargs={"pin_slug": self.root.slug}))
        names = {dp["name"] for dp in response.json()["detail_pins"]}
        self.assertEqual(names, {"Child"})

    def test_children_flag_returns_full_subtree_with_owner_names(self) -> None:
        response = self.client.get(reverse("pin.detail_pins.json", kwargs={"pin_slug": self.root.slug}), {"children": "1"})
        by_name = {dp["name"]: dp for dp in response.json()["detail_pins"]}
        self.assertEqual(set(by_name), {"Child", "Grandchild"})
        self.assertNotIn("owner_name", by_name["Child"])
        self.assertEqual(by_name["Grandchild"]["owner_name"], "Child")

    def test_detail_json_links_to_child_detail_page(self) -> None:
        response = self.client.get(reverse("pin.detail_pins.json", kwargs={"pin_slug": self.root.slug}))
        entry = response.json()["detail_pins"][0]
        self.assertIn("/dashboard/map/pin/", entry["url"])


class DetailPinCoordinateDedupTests(TestCase):
    """Detail pins placed near each other must keep distinct coordinates.

    ``Location.objects.get_nearby_or_create``'s default 50m proximity dedup
    would otherwise snap two nearby detail pins (or a detail pin and its own
    parent) onto the same Location, collapsing their marker coordinates
    together - reported after several detail pins placed around a map all
    ended up stacked on one point.
    """

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.root = _make_pin(self.profile, name="Root", location=baker.make(Location, latitude=42.0, longitude=-73.0))
        self.root.slug = self.root.ensure_slug()

    def _create_detail_pin(self, name: str, latitude: float, longitude: float) -> Pin:
        response = self.client.post(
            reverse("pin.detail_pins", kwargs={"pin_slug": self.root.slug}),
            data=json.dumps({"name": name, "latitude": latitude, "longitude": longitude}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        return Pin.objects.get(uuid=response.json()["uuid"])

    def test_nearby_detail_pins_keep_distinct_coordinates(self) -> None:
        # ~20m apart - well within get_nearby_or_create's default 50m dedup radius.
        first = self._create_detail_pin("First", 42.00010, -73.00010)
        second = self._create_detail_pin("Second", 42.00020, -73.00020)
        self.assertNotEqual(first.location_id, second.location_id)
        self.assertNotEqual((first.effective_latitude, first.effective_longitude), (second.effective_latitude, second.effective_longitude))

    def test_detail_pin_near_parent_keeps_its_own_coordinates(self) -> None:
        child = self._create_detail_pin("Nearby child", 42.00010, -73.00010)
        self.assertNotEqual(child.location_id, self.root.location_id)
        self.assertNotEqual((child.effective_latitude, child.effective_longitude), (self.root.effective_latitude, self.root.effective_longitude))

    def test_moving_detail_pin_near_another_keeps_distinct_coordinates(self) -> None:
        first = self._create_detail_pin("First", 42.00010, -73.00010)
        second = self._create_detail_pin("Second", 42.01000, -73.01000)
        response = self.client.post(
            reverse("pin.detail_pin.edit", kwargs={"pin_slug": self.root.slug, "detail_pin_uuid": second.uuid}),
            data=json.dumps({"latitude": 42.00011, "longitude": -73.00011}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        second.refresh_from_db()
        self.assertNotEqual(first.location_id, second.location_id)


class VisitHistoryChildrenTests(TestCase):
    """?children=1 folds child-pin visits into the parent's visit history."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.root = _make_pin(self.profile, name="Root")
        self.root.slug = self.root.ensure_slug()
        self.child = _make_pin(self.profile, name="Turbine Hall", parent_pin=self.root)
        self.child.slug = self.child.ensure_slug()
        baker.make(PinVisit, pin=self.root, notes="root visit")
        baker.make(PinVisit, pin=self.child, notes="child visit")

    def test_default_shows_only_own_visits(self) -> None:
        response = self.client.get(reverse("pin.visits", kwargs={"pin_slug": self.root.slug}))
        content = response.content.decode()
        self.assertIn("root visit", content)
        self.assertNotIn("child visit", content)

    def test_children_flag_includes_labelled_child_visits(self) -> None:
        response = self.client.get(reverse("pin.visits", kwargs={"pin_slug": self.root.slug}), {"children": "1"})
        content = response.content.decode()
        self.assertIn("root visit", content)
        self.assertIn("child visit", content)
        self.assertIn("Turbine Hall", content)


class PinShareBundleTests(TestCase):
    """Sharing with include_children bundles every sub pin as its own share."""

    def setUp(self) -> None:
        self.sender_user = baker.make(User)
        self.sender = self.sender_user.profile
        self.recipient_user = baker.make(User)
        self.recipient = self.recipient_user.profile
        Friendship.objects.create(from_profile=self.sender, to_profile=self.recipient, status=FriendshipStatus.ACCEPTED)
        self.client.force_login(self.sender_user)
        self.root = _make_pin(self.sender, name="Steel Works")
        self.root.slug = self.root.ensure_slug()
        self.child = _make_pin(self.sender, name="Blast Furnace", parent_pin=self.root, icon="factory")
        self.grandchild = _make_pin(self.sender, name="Control Room", parent_pin=self.child)

    def _share(self, include_children: bool):
        data = {"profile_id": self.recipient.pk}
        if include_children:
            data["include_children"] = "1"
        return self.client.post(reverse("pin.share.send", kwargs={"pin_slug": self.root.slug}), data)

    def test_creates_a_share_row_per_sub_pin(self) -> None:
        self._share(include_children=True)
        root_share = PinShare.objects.get(pin=self.root)
        bundled_pins = set(root_share.bundled_shares.values_list("pin_id", flat=True))
        self.assertEqual(bundled_pins, {self.child.pk, self.grandchild.pk})

    def test_without_flag_no_bundle_is_created(self) -> None:
        self._share(include_children=False)
        root_share = PinShare.objects.get(pin=self.root)
        self.assertEqual(root_share.bundled_shares.count(), 0)

    def test_notification_mentions_sub_pins(self) -> None:
        self._share(include_children=True)
        root_share = PinShare.objects.select_related("notification").get(pin=self.root)
        self.assertIn("2 child pins", root_share.notification.message)

    def test_accept_recreates_hierarchy_for_recipient(self) -> None:
        self._share(include_children=True)
        root_share = PinShare.objects.get(pin=self.root)
        self.client.force_login(self.recipient_user)
        self.client.post(reverse("pin.share.respond", kwargs={"share_id": root_share.pk}), {"action": "accept"})

        new_root = Pin.objects.get(profile=self.recipient, name="Steel Works")
        new_child = Pin.objects.get(profile=self.recipient, name="Blast Furnace")
        new_grandchild = Pin.objects.get(profile=self.recipient, name="Control Room")
        self.assertIsNone(new_root.parent_pin_id)
        self.assertEqual(new_child.parent_pin_id, new_root.pk)
        self.assertEqual(new_grandchild.parent_pin_id, new_child.pk)
        self.assertEqual(new_child.icon, "factory")

    def test_reject_rejects_the_whole_bundle(self) -> None:
        self._share(include_children=True)
        root_share = PinShare.objects.get(pin=self.root)
        self.client.force_login(self.recipient_user)
        self.client.post(reverse("pin.share.respond", kwargs={"share_id": root_share.pk}), {"action": "reject"})
        statuses = set(PinShare.objects.filter(to_profile=self.recipient).values_list("status", flat=True))
        self.assertEqual(statuses, {PinShareStatus.REJECTED})
        self.assertFalse(Pin.objects.filter(profile=self.recipient).exists())


class VisitedLabelPropagationTests(TestCase):
    """Adding the Visited status label to a child pin stamps all its ancestors."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.visited = baker.make(Label, kind=KIND_STATUS, name="Visited", profile=self.profile)
        self.root = _make_pin(self.profile)
        self.child = _make_pin(self.profile, parent_pin=self.root)
        self.grandchild = _make_pin(self.profile, parent_pin=self.child)

    def test_label_cascades_to_all_ancestors(self) -> None:
        self.grandchild.labels.add(self.visited)
        self.assertIn(self.visited, self.child.labels.all())
        self.assertIn(self.visited, self.root.labels.all())

    def test_root_pin_label_does_not_cascade_down(self) -> None:
        self.root.labels.add(self.visited)
        self.assertNotIn(self.visited, self.child.labels.all())

    def test_other_status_labels_do_not_cascade(self) -> None:
        other = baker.make(Label, kind=KIND_STATUS, name="Demolished", profile=self.profile)
        self.grandchild.labels.add(other)
        self.assertNotIn(other, self.root.labels.all())
