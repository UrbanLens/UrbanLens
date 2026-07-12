"""DB-backed tests for map-based pin-share detection and "Add to my maps".

Covers:
- detect_shared_pins - zoomed-in (viewport-only) vs zoomed-out (markup-gated)
  matching, scoped to the sender's own root pins.
- share_markup_map_with_profile / _record_detected_share - creates DETECTED
  PinShare rows, deduplicated per (pin, recipient), reusing the same
  parent_share chain rule as explicit shares.
- PinShare.chain_share_count / MemoriesSharingView pick up detected shares
  transparently.
- clone_markup_map / MarkupMapCloneView - "Add to my maps" clone + visibility
  gating.
- MarkupMapShareCreateView - friends-only standalone map sharing.
- PinShareCreateView - optional map attachment validation.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.test import override_settings
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.markup.meta import MarkupType
from urbanlens.dashboard.models.markup.model import MarkupMap, PinMarkup
from urbanlens.dashboard.models.markup.share import MarkupMapShare
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_share import PinShare, PinShareOrigin, PinShareStatus
from urbanlens.dashboard.services.map_pin_share_detection import detect_shared_pins
from urbanlens.dashboard.services.map_sharing import clone_markup_map, share_markup_map_with_profile

# Fixed test coordinates - Manhattan-ish, nowhere near a pole/antimeridian.
_LAT, _LNG = 40.0, -74.0


def _befriend(a, b) -> None:
    Friendship.objects.create(from_profile=a, to_profile=b, status=FriendshipStatus.ACCEPTED)


class _MapShareTestCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.users = {name: baker.make(User, username=name) for name in "abc"}
        self.profiles = {name: user.profile for name, user in self.users.items()}
        self.location = baker.make(Location, latitude=f"{_LAT:.6f}", longitude=f"{_LNG:.6f}", official_name="Old Mill")
        self.pin = Pin.objects.create(profile=self.profiles["a"], location=self.location)

    def _map(self, *, zoom: float, center_lat: float = _LAT, center_lng: float = _LNG, profile=None) -> MarkupMap:
        return MarkupMap.objects.create(
            profile=profile or self.profiles["a"],
            center_latitude=center_lat,
            center_longitude=center_lng,
            zoom=zoom,
        )

    def _markup_item(self, markup_map: MarkupMap, markup_type: str, geometry: dict) -> PinMarkup:
        return PinMarkup.objects.create(parent_map=markup_map, profile=markup_map.profile, markup_type=markup_type, geometry=geometry)


# -- detect_shared_pins -------------------------------------------------------------

@override_settings(UL_MAP_SHARE_ZOOM_THRESHOLD=14)
class DetectSharedPinsTests(_MapShareTestCase):
    def test_zoomed_in_includes_pin_in_view_with_no_markup(self) -> None:
        markup_map = self._map(zoom=16)
        matches = detect_shared_pins(markup_map, self.profiles["a"])
        self.assertEqual(matches, [self.pin])

    def test_zoomed_in_excludes_pin_out_of_view(self) -> None:
        markup_map = self._map(zoom=16, center_lat=10.0, center_lng=10.0)
        matches = detect_shared_pins(markup_map, self.profiles["a"])
        self.assertEqual(matches, [])

    def test_zoomed_out_with_no_markup_matches_nothing(self) -> None:
        markup_map = self._map(zoom=4)
        matches = detect_shared_pins(markup_map, self.profiles["a"])
        self.assertEqual(matches, [])

    def test_zoomed_out_pin_marker_in_boundary_matches(self) -> None:
        markup_map = self._map(zoom=4)
        self._markup_item(markup_map, MarkupType.PIN, {"type": "Point", "coordinates": [_LNG, _LAT]})
        matches = detect_shared_pins(markup_map, self.profiles["a"])
        self.assertEqual(matches, [self.pin])

    def test_zoomed_out_arrow_pointing_at_pin_matches(self) -> None:
        markup_map = self._map(zoom=4)
        # Tail 1 degree south of the pin, head at the pin - points due north at it.
        self._markup_item(markup_map, MarkupType.ARROW, {"type": "LineString", "coordinates": [[_LNG, _LAT - 1.0], [_LNG, _LAT]]})
        matches = detect_shared_pins(markup_map, self.profiles["a"])
        self.assertEqual(matches, [self.pin])

    def test_zoomed_out_arrow_pointing_away_does_not_match(self) -> None:
        markup_map = self._map(zoom=4)
        # Tail near the pin, head pointing due east, away from the pin.
        self._markup_item(markup_map, MarkupType.ARROW, {"type": "LineString", "coordinates": [[_LNG, _LAT], [_LNG + 2.0, _LAT]]})
        matches = detect_shared_pins(markup_map, self.profiles["a"])
        self.assertEqual(matches, [])

    def test_zoomed_out_polygon_overlap_matches(self) -> None:
        markup_map = self._map(zoom=4)
        delta = 0.01
        ring = [
            [_LNG - delta, _LAT - delta],
            [_LNG + delta, _LAT - delta],
            [_LNG + delta, _LAT + delta],
            [_LNG - delta, _LAT + delta],
            [_LNG - delta, _LAT - delta],
        ]
        self._markup_item(markup_map, MarkupType.POLYGON, {"type": "Polygon", "coordinates": [ring]})
        matches = detect_shared_pins(markup_map, self.profiles["a"])
        self.assertEqual(matches, [self.pin])

    def test_only_senders_own_pins_are_considered(self) -> None:
        other_pin = Pin.objects.create(profile=self.profiles["b"], location=self.location)
        markup_map = self._map(zoom=16, profile=self.profiles["b"])
        matches = detect_shared_pins(markup_map, self.profiles["b"])
        self.assertEqual(matches, [other_pin])
        self.assertNotIn(self.pin, matches)

    def test_child_pins_are_excluded(self) -> None:
        Pin.objects.create(profile=self.profiles["a"], location=self.location, parent_pin=self.pin)
        markup_map = self._map(zoom=16)
        matches = detect_shared_pins(markup_map, self.profiles["a"])
        self.assertEqual(matches, [self.pin])

    def test_no_saved_viewport_matches_nothing(self) -> None:
        markup_map = MarkupMap.objects.create(profile=self.profiles["a"])
        self.assertEqual(detect_shared_pins(markup_map, self.profiles["a"]), [])


# -- share_markup_map_with_profile / dedup / chaining --------------------------------

@override_settings(UL_MAP_SHARE_ZOOM_THRESHOLD=14)
class ShareMarkupMapWithProfileTests(_MapShareTestCase):
    def test_creates_detected_share_for_matched_pin(self) -> None:
        markup_map = self._map(zoom=16)
        shares = share_markup_map_with_profile(self.profiles["a"], self.profiles["b"], markup_map)
        self.assertEqual(len(shares), 1)
        share = PinShare.objects.get(pin=self.pin, to_profile=self.profiles["b"])
        self.assertEqual(share.origin, PinShareOrigin.MAP_DETECTED)
        self.assertEqual(share.status, PinShareStatus.DETECTED)
        self.assertEqual(share.detected_via_map_id, markup_map.pk)
        self.assertFalse(share.is_actionable)

    def test_resending_same_map_does_not_duplicate(self) -> None:
        markup_map = self._map(zoom=16)
        share_markup_map_with_profile(self.profiles["a"], self.profiles["b"], markup_map)
        second = share_markup_map_with_profile(self.profiles["a"], self.profiles["b"], markup_map)
        self.assertEqual(second, [])
        self.assertEqual(PinShare.objects.filter(pin=self.pin, to_profile=self.profiles["b"]).count(), 1)

    def test_different_map_covering_same_pin_does_not_duplicate(self) -> None:
        first_map = self._map(zoom=16)
        share_markup_map_with_profile(self.profiles["a"], self.profiles["b"], first_map)
        second_map = self._map(zoom=16)
        second = share_markup_map_with_profile(self.profiles["a"], self.profiles["b"], second_map)
        self.assertEqual(second, [])
        self.assertEqual(PinShare.objects.filter(pin=self.pin, to_profile=self.profiles["b"]).count(), 1)

    def test_parent_share_chains_through_source_share(self) -> None:
        root_share = PinShare.objects.create(pin=self.pin, from_profile=self.profiles["c"], to_profile=self.profiles["a"], status=PinShareStatus.ACCEPTED)
        self.pin.source_share = root_share
        self.pin.save(update_fields=["source_share"])
        markup_map = self._map(zoom=16)
        share_markup_map_with_profile(self.profiles["a"], self.profiles["b"], markup_map)
        detected = PinShare.objects.get(pin=self.pin, to_profile=self.profiles["b"])
        self.assertEqual(detected.parent_share_id, root_share.pk)

    def test_parent_share_falls_back_to_inferred_source_share(self) -> None:
        inferred_share = PinShare.objects.create(pin=self.pin, from_profile=self.profiles["c"], to_profile=self.profiles["a"], status=PinShareStatus.DETECTED, origin=PinShareOrigin.MAP_DETECTED)
        self.pin.inferred_source_share = inferred_share
        self.pin.save(update_fields=["inferred_source_share"])
        markup_map = self._map(zoom=16)
        share_markup_map_with_profile(self.profiles["a"], self.profiles["b"], markup_map)
        detected = PinShare.objects.get(pin=self.pin, to_profile=self.profiles["b"])
        self.assertEqual(detected.parent_share_id, inferred_share.pk)


class ChainShareCountIncludesDetectedTests(_MapShareTestCase):
    def test_chain_share_count_includes_detected_share(self) -> None:
        explicit_share = PinShare.objects.create(pin=self.pin, from_profile=self.profiles["a"], to_profile=self.profiles["b"], status=PinShareStatus.PENDING)
        # A later detected share downstream of the explicit one (e.g. b forwarded
        # a map revealing the same pin to c) should still count toward the chain.
        PinShare.objects.create(pin=self.pin, from_profile=self.profiles["b"], to_profile=self.profiles["c"], parent_share_id=explicit_share.pk, origin=PinShareOrigin.MAP_DETECTED, status=PinShareStatus.DETECTED)
        self.assertEqual(PinShare.chain_share_count([explicit_share.pk]), 2)

    def test_memories_sharing_page_includes_detected_share(self) -> None:
        share = PinShare.objects.create(pin=self.pin, from_profile=self.profiles["a"], to_profile=self.profiles["b"], origin=PinShareOrigin.MAP_DETECTED, status=PinShareStatus.DETECTED)
        self.client.force_login(self.users["a"])
        response = self.client.get(reverse("memories.sharing"))
        self.assertEqual(response.status_code, 200)
        groups = response.context["share_groups"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["chain_total"], 1)
        self.assertIn(share, PinShare.objects.filter(pin=self.pin))


# -- clone_markup_map / MarkupMapCloneView -------------------------------------------

class CloneMarkupMapTests(_MapShareTestCase):
    def test_clone_reproduces_snapshot_and_sets_provenance(self) -> None:
        source = self._map(zoom=16)
        self._markup_item(source, MarkupType.PIN, {"type": "Point", "coordinates": [_LNG, _LAT]})
        clone = clone_markup_map(source, self.profiles["b"], sender=self.profiles["a"])
        self.assertEqual(clone.profile_id, self.profiles["b"].pk)
        self.assertEqual(clone.cloned_from_id, source.pk)
        self.assertEqual(clone.shared_by_id, self.profiles["a"].pk)
        self.assertEqual(clone.to_snapshot()["markup"], source.to_snapshot()["markup"])

    def test_clone_view_requires_visibility(self) -> None:
        source = self._map(zoom=16)
        self.client.force_login(self.users["b"])
        response = self.client.post(reverse("markup_map.clone", kwargs={"map_uuid": source.uuid}))
        self.assertEqual(response.status_code, 404)

    def test_clone_view_via_dm_attachment(self) -> None:
        source = self._map(zoom=16)
        from urbanlens.dashboard.models.direct_messages.model import DirectMessage

        DirectMessage.objects.create(sender=self.profiles["a"], recipient=self.profiles["b"], body="check this out", markup_map=source)
        self.client.force_login(self.users["b"])
        response = self.client.post(reverse("markup_map.clone", kwargs={"map_uuid": source.uuid}))
        self.assertEqual(response.status_code, 302)
        clone = MarkupMap.objects.get(profile=self.profiles["b"], cloned_from=source)
        self.assertEqual(clone.shared_by_id, self.profiles["a"].pk)

    def test_clone_view_is_idempotent(self) -> None:
        source = self._map(zoom=16)
        from urbanlens.dashboard.models.direct_messages.model import DirectMessage

        DirectMessage.objects.create(sender=self.profiles["a"], recipient=self.profiles["b"], body="check this out", markup_map=source)
        self.client.force_login(self.users["b"])
        self.client.post(reverse("markup_map.clone", kwargs={"map_uuid": source.uuid}))
        self.client.post(reverse("markup_map.clone", kwargs={"map_uuid": source.uuid}))
        self.assertEqual(MarkupMap.objects.filter(profile=self.profiles["b"], cloned_from=source).count(), 1)

    def test_cannot_clone_own_map(self) -> None:
        source = self._map(zoom=16)
        self.client.force_login(self.users["a"])
        response = self.client.post(reverse("markup_map.clone", kwargs={"map_uuid": source.uuid}))
        self.assertEqual(response.status_code, 400)


# -- MarkupMapShareCreateView ---------------------------------------------------------

class MarkupMapShareCreateViewTests(_MapShareTestCase):
    def test_rejects_non_friend(self) -> None:
        source = self._map(zoom=16)
        self.client.force_login(self.users["a"])
        response = self.client.post(reverse("markup_map.share.send", kwargs={"map_uuid": source.uuid}), {"profile_id": self.profiles["b"].pk})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(MarkupMapShare.objects.filter(markup_map=source).exists())

    def test_shares_with_connected_friend(self) -> None:
        _befriend(self.profiles["a"], self.profiles["b"])
        source = self._map(zoom=16)
        self.client.force_login(self.users["a"])
        response = self.client.post(reverse("markup_map.share.send", kwargs={"map_uuid": source.uuid}), {"profile_id": self.profiles["b"].pk})
        self.assertEqual(response.status_code, 200)
        share = MarkupMapShare.objects.get(markup_map=source)
        self.assertEqual(share.to_profile_id, self.profiles["b"].pk)
        self.assertIsNotNone(share.notification_id)
        # Sharing a zoomed-in map that shows the sender's own pin should also
        # record a detected PinShare via the same central hook.
        self.assertTrue(PinShare.objects.filter(pin=self.pin, to_profile=self.profiles["b"], origin=PinShareOrigin.MAP_DETECTED).exists())


# -- PinShareCreateView map attachment ------------------------------------------------

class PinShareCreateViewMapAttachmentTests(_MapShareTestCase):
    def test_rejects_map_not_owned_by_sender(self) -> None:
        _befriend(self.profiles["a"], self.profiles["b"])
        other_map = self._map(zoom=16, profile=self.profiles["c"])
        self.client.force_login(self.users["a"])
        response = self.client.post(
            reverse("pin.share.send", kwargs={"pin_slug": self.pin.slug}),
            {"profile_id": self.profiles["b"].pk, "markup_map_uuid": str(other_map.uuid)},
        )
        self.assertEqual(response.status_code, 200)
        share = PinShare.objects.get(pin=self.pin, to_profile=self.profiles["b"])
        self.assertIsNone(share.markup_map_id)

    def test_attaches_own_map(self) -> None:
        _befriend(self.profiles["a"], self.profiles["b"])
        own_map = self._map(zoom=16)
        self.client.force_login(self.users["a"])
        response = self.client.post(
            reverse("pin.share.send", kwargs={"pin_slug": self.pin.slug}),
            {"profile_id": self.profiles["b"].pk, "markup_map_uuid": str(own_map.uuid)},
        )
        self.assertEqual(response.status_code, 200)
        share = PinShare.objects.get(pin=self.pin, to_profile=self.profiles["b"])
        self.assertEqual(share.markup_map_id, own_map.pk)
