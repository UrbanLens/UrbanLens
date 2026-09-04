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

import math

from django.contrib.auth.models import User
from django.contrib.gis.geos import Point
from django.test import override_settings
from django.urls import reverse
from hypothesis import given, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.markup.meta import MarkupType
from urbanlens.dashboard.models.markup.model import MarkupMap, PinMarkup
from urbanlens.dashboard.models.markup.share import MarkupMapShare
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_share import PinShare, PinShareOrigin, PinShareStatus
from urbanlens.dashboard.models.profile.meta import VisibilityChoice
from urbanlens.dashboard.services.profile.identity_visibility import resolve_visible_identity
from urbanlens.dashboard.services.sharing.map_pin_share_detection import (
    arrow_points_toward,
    detect_shared_pins,
    sync_pin_inferences,
)
from urbanlens.dashboard.services.sharing.map_sharing import clone_markup_map, share_markup_map_with_profile

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
        return PinMarkup.objects.create(
            parent_map=markup_map, profile=markup_map.profile, markup_type=markup_type, geometry=geometry
        )


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

    def test_zoomed_in_excludes_an_in_frame_pin_the_map_is_not_aimed_at(self) -> None:
        """Being on screen is not being shared.

        A snapshot carries the viewport and the drawn shapes - never the
        sender's pins - so a recipient cannot learn about a pin the sender
        neither centred on nor drew anything near. Whole-frame containment
        recorded a share for every pin across roughly 9 x 7 km at the default
        threshold zoom, which then surfaced on the recipient's Sharing page.
        """
        off_centre = baker.make(Location, latitude=f"{_LAT + 0.004:.6f}", longitude=f"{_LNG:.6f}")
        bystander = Pin.objects.create(profile=self.profiles["a"], location=off_centre)
        markup_map = self._map(zoom=16)

        matches = detect_shared_pins(markup_map, self.profiles["a"])

        self.assertIn(self.pin, matches, "the pin the view is centred on is still shared")
        self.assertNotIn(bystander, matches)

    def test_zoomed_in_still_matches_an_off_centre_pin_the_markup_calls_out(self) -> None:
        """Drawing on a pin shares it wherever it sits in the frame."""
        off_centre = baker.make(Location, latitude=f"{_LAT + 0.004:.6f}", longitude=f"{_LNG:.6f}")
        called_out = Pin.objects.create(profile=self.profiles["a"], location=off_centre)
        markup_map = self._map(zoom=16)
        self._markup_item(markup_map, MarkupType.PIN, {"type": "Point", "coordinates": [_LNG, _LAT + 0.004]})

        matches = detect_shared_pins(markup_map, self.profiles["a"])

        self.assertIn(called_out, matches)

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
        self._markup_item(
            markup_map, MarkupType.ARROW, {"type": "LineString", "coordinates": [[_LNG, _LAT - 1.0], [_LNG, _LAT]]}
        )
        matches = detect_shared_pins(markup_map, self.profiles["a"])
        self.assertEqual(matches, [self.pin])

    def test_zoomed_out_arrow_pointing_away_does_not_match(self) -> None:
        markup_map = self._map(zoom=4)
        # Tail near the pin, head pointing due east, away from the pin.
        self._markup_item(
            markup_map, MarkupType.ARROW, {"type": "LineString", "coordinates": [[_LNG, _LAT], [_LNG + 2.0, _LAT]]}
        )
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


class ArrowTailDegeneracyTests(SimpleTestCase):
    """An arrow whose tail sits on the target points nowhere in particular.

    ``bearing_degrees`` from a point to itself is not merely arbitrary, it is
    unstable: a pin's boundary centroid lands ~1e-14 degrees off the tail
    through ordinary float error, which used to yield a confident angle that
    fell inside the 35-degree tolerance often enough to record DETECTED shares
    of pins the sender never called out.
    """

    @staticmethod
    def _arrow(coordinates: list[list[float]]) -> PinMarkup:
        # Unsaved: arrow_points_toward only ever reads .geometry.
        return PinMarkup(markup_type=MarkupType.ARROW, geometry={"type": "LineString", "coordinates": coordinates})

    @given(
        heading=st.floats(min_value=0.0, max_value=359.0),
        jitter_x=st.floats(min_value=-1e-13, max_value=1e-13),
        jitter_y=st.floats(min_value=-1e-13, max_value=1e-13),
    )
    def test_target_on_the_tail_never_matches_whichever_way_the_arrow_points(
        self, heading: float, jitter_x: float, jitter_y: float
    ) -> None:
        head_lng = _LNG + 2.0 * math.sin(math.radians(heading))
        head_lat = _LAT + 2.0 * math.cos(math.radians(heading))
        arrow = self._arrow([[_LNG, _LAT], [head_lng, head_lat]])
        target = Point(_LNG + jitter_x, _LAT + jitter_y, srid=4326)
        self.assertFalse(arrow_points_toward(arrow, target))

    def test_a_genuinely_distant_target_still_matches(self) -> None:
        """The guard must not swallow the real case it sits next to."""
        arrow = self._arrow([[_LNG, _LAT - 1.0], [_LNG, _LAT]])
        self.assertTrue(arrow_points_toward(arrow, Point(_LNG, _LAT, srid=4326)))


# -- sync_pin_inferences / MarkupMap.inferred_pins ------------------------------------


@override_settings(UL_MAP_SHARE_ZOOM_THRESHOLD=14)
class SyncPinInferencesTests(_MapShareTestCase):
    def test_persists_detected_matches(self) -> None:
        markup_map = self._map(zoom=16)
        pins = sync_pin_inferences(markup_map)
        self.assertEqual(pins, [self.pin])
        self.assertEqual(list(markup_map.inferred_pins.all()), [self.pin])
        self.assertIn(markup_map, self.pin.inferred_maps.all())

    def test_resync_drops_matches_that_no_longer_hold(self) -> None:
        markup_map = self._map(zoom=16)
        sync_pin_inferences(markup_map)
        self.assertEqual(list(markup_map.inferred_pins.all()), [self.pin])

        # Pan away from the pin, then resync - the stale match should be removed.
        markup_map.center_latitude, markup_map.center_longitude = 10.0, 10.0
        markup_map.save(update_fields=["center_latitude", "center_longitude"])
        sync_pin_inferences(markup_map)
        self.assertEqual(list(markup_map.inferred_pins.all()), [])

    def test_independent_of_explicit_pin_link(self) -> None:
        """Clearing MarkupMap.pin must never touch inferred_pins, and vice versa."""
        markup_map = self._map(zoom=16)
        markup_map.pin = self.pin
        markup_map.save(update_fields=["pin"])
        sync_pin_inferences(markup_map)
        self.assertEqual(list(markup_map.inferred_pins.all()), [self.pin])

        markup_map.pin = None
        markup_map.save(update_fields=["pin"])
        markup_map.refresh_from_db()
        self.assertIsNone(markup_map.pin_id)
        self.assertEqual(list(markup_map.inferred_pins.all()), [self.pin])

    def test_saving_map_or_item_auto_syncs_via_signal(self) -> None:
        markup_map = self._map(zoom=4)
        with self.captureOnCommitCallbacks(execute=True):
            self._markup_item(markup_map, MarkupType.PIN, {"type": "Point", "coordinates": [_LNG, _LAT]})
        self.assertEqual(list(markup_map.inferred_pins.all()), [self.pin])


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
        self.assertEqual(list(markup_map.inferred_pins.all()), [self.pin])

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
        root_share = PinShare.objects.create(
            pin=self.pin, from_profile=self.profiles["c"], to_profile=self.profiles["a"], status=PinShareStatus.ACCEPTED
        )
        self.pin.source_share = root_share
        self.pin.save(update_fields=["source_share"])
        markup_map = self._map(zoom=16)
        share_markup_map_with_profile(self.profiles["a"], self.profiles["b"], markup_map)
        detected = PinShare.objects.get(pin=self.pin, to_profile=self.profiles["b"])
        self.assertEqual(detected.parent_share_id, root_share.pk)

    def test_parent_share_falls_back_to_inferred_source_share(self) -> None:
        inferred_share = PinShare.objects.create(
            pin=self.pin,
            from_profile=self.profiles["c"],
            to_profile=self.profiles["a"],
            status=PinShareStatus.DETECTED,
            origin=PinShareOrigin.MAP_DETECTED,
        )
        self.pin.inferred_source_share = inferred_share
        self.pin.save(update_fields=["inferred_source_share"])
        markup_map = self._map(zoom=16)
        share_markup_map_with_profile(self.profiles["a"], self.profiles["b"], markup_map)
        detected = PinShare.objects.get(pin=self.pin, to_profile=self.profiles["b"])
        self.assertEqual(detected.parent_share_id, inferred_share.pk)


class ChainShareCountIncludesDetectedTests(_MapShareTestCase):
    def test_chain_share_count_includes_detected_share(self) -> None:
        explicit_share = PinShare.objects.create(
            pin=self.pin, from_profile=self.profiles["a"], to_profile=self.profiles["b"], status=PinShareStatus.PENDING
        )
        # A later detected share downstream of the explicit one (e.g. b forwarded
        # a map revealing the same pin to c) should still count toward the chain.
        PinShare.objects.create(
            pin=self.pin,
            from_profile=self.profiles["b"],
            to_profile=self.profiles["c"],
            parent_share_id=explicit_share.pk,
            origin=PinShareOrigin.MAP_DETECTED,
            status=PinShareStatus.DETECTED,
        )
        self.assertEqual(PinShare.chain_share_count([explicit_share.pk]), 2)

    def test_memories_sharing_page_includes_detected_share(self) -> None:
        share = PinShare.objects.create(
            pin=self.pin,
            from_profile=self.profiles["a"],
            to_profile=self.profiles["b"],
            origin=PinShareOrigin.MAP_DETECTED,
            status=PinShareStatus.DETECTED,
        )
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

        DirectMessage.objects.create(
            sender=self.profiles["a"], recipient=self.profiles["b"], body="check this out", markup_map=source
        )
        self.client.force_login(self.users["b"])
        response = self.client.post(reverse("markup_map.clone", kwargs={"map_uuid": source.uuid}))
        self.assertEqual(response.status_code, 302)
        clone = MarkupMap.objects.get(profile=self.profiles["b"], cloned_from=source)
        self.assertEqual(clone.shared_by_id, self.profiles["a"].pk)

    def test_clone_view_is_idempotent(self) -> None:
        source = self._map(zoom=16)
        from urbanlens.dashboard.models.direct_messages.model import DirectMessage

        DirectMessage.objects.create(
            sender=self.profiles["a"], recipient=self.profiles["b"], body="check this out", markup_map=source
        )
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
        response = self.client.post(
            reverse("markup_map.share.send", kwargs={"map_uuid": source.uuid}), {"profile_id": self.profiles["b"].pk}
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(MarkupMapShare.objects.filter(markup_map=source).exists())

    def test_shares_with_connected_friend(self) -> None:
        _befriend(self.profiles["a"], self.profiles["b"])
        source = self._map(zoom=16)
        self.client.force_login(self.users["a"])
        response = self.client.post(
            reverse("markup_map.share.send", kwargs={"map_uuid": source.uuid}), {"profile_id": self.profiles["b"].pk}
        )
        self.assertEqual(response.status_code, 200)
        share = MarkupMapShare.objects.get(markup_map=source)
        self.assertEqual(share.to_profile_id, self.profiles["b"].pk)
        self.assertIsNotNone(share.notification_id)
        # Sharing a zoomed-in map that shows the sender's own pin should also
        # record a detected PinShare via the same central hook.
        self.assertTrue(
            PinShare.objects.filter(
                pin=self.pin, to_profile=self.profiles["b"], origin=PinShareOrigin.MAP_DETECTED
            ).exists()
        )

    def test_notification_masks_hidden_sender(self) -> None:
        """Regression: the notification text interpolated ``sender.username``
        directly, bypassing the recipient-scoped masking every other identity
        surface (thread renders, DM/group live payloads) already applies."""
        _befriend(self.profiles["a"], self.profiles["b"])
        self.users["a"].username = "hidden-sender"
        self.users["a"].save(update_fields=["username"])
        self.profiles["a"].profile_visibility = VisibilityChoice.NO_ONE
        self.profiles["a"].save(update_fields=["profile_visibility"])
        source = self._map(zoom=16)
        self.client.force_login(self.users["a"])
        response = self.client.post(
            reverse("markup_map.share.send", kwargs={"map_uuid": source.uuid}), {"profile_id": self.profiles["b"].pk}
        )
        self.assertEqual(response.status_code, 200)
        notification = MarkupMapShare.objects.get(markup_map=source).notification
        expected_name = resolve_visible_identity(self.profiles["b"], self.profiles["a"])["display_name"]
        self.assertIn(expected_name, notification.message)
        self.assertNotIn(self.profiles["a"].username, notification.message)


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

    def test_notification_masks_hidden_sender(self) -> None:
        _befriend(self.profiles["a"], self.profiles["b"])
        self.users["a"].username = "hidden-sender"
        self.users["a"].save(update_fields=["username"])
        self.profiles["a"].profile_visibility = VisibilityChoice.NO_ONE
        self.profiles["a"].save(update_fields=["profile_visibility"])
        self.client.force_login(self.users["a"])
        response = self.client.post(
            reverse("pin.share.send", kwargs={"pin_slug": self.pin.slug}), {"profile_id": self.profiles["b"].pk}
        )
        self.assertEqual(response.status_code, 200)
        notification = PinShare.objects.get(pin=self.pin, to_profile=self.profiles["b"]).notification
        expected_name = resolve_visible_identity(self.profiles["b"], self.profiles["a"])["display_name"]
        self.assertIn(expected_name, notification.message)
        self.assertNotIn(self.profiles["a"].username, notification.message)
