"""Tests for the standalone MarkupMap model, snapshot conversion, and endpoints.

Covers:
- PinMarkup.from_snapshot_shape / to_snapshot_shape round-trips (property-based).
- sanitize_map_data's layer_mode/show_borders handling.
- MarkupMap.replace_items_from_snapshot + to_snapshot.
- services.map_snapshot.materialize_markup_map create/update/remove semantics.
- MarkupMapQuerySet.unattached().
- The /markup-maps/ endpoints: create, item CRUD, view-state, delete, and
  ownership enforcement.
- Check-in creation linking a draft map via the ``markup_map`` POST field.
"""

from __future__ import annotations

import datetime
import json

from django.urls import reverse
from django.utils import timezone
from django.utils.dateformat import format as format_date
from hypothesis import given, settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.markup.model import MarkupMap, PinMarkup
from urbanlens.dashboard.services.map_snapshot import default_markup_map_title, materialize_markup_map, sanitize_map_data

# Latitudes stay away from the poles so circle radius→edge conversion stays finite.
_lat = st.floats(min_value=-84, max_value=84, allow_nan=False, allow_infinity=False)
_lng = st.floats(min_value=-179, max_value=179, allow_nan=False, allow_infinity=False)
_latlng = st.tuples(_lat, _lng).map(list)


def _shape(shape_type: str, latlngs: list, **extra) -> dict:
    base = {
        "type": shape_type,
        "latlngs": latlngs,
        "color": "#2196f3",
        "stroke_width": 4,
        "fill_opacity": 60,
        "border_opacity": 90,
    }
    base.update(extra)
    return base


class ShapeRoundTripTests(TestCase):
    """from_snapshot_shape → to_snapshot_shape preserves the drawing."""

    @settings(max_examples=25, deadline=None)
    @given(latlngs=st.lists(_latlng, min_size=2, max_size=6), shape_type=st.sampled_from(["line", "arrow"]))
    def test_line_and_arrow_round_trip(self, latlngs: list, shape_type: str) -> None:
        item = PinMarkup.from_snapshot_shape(_shape(shape_type, latlngs))
        assert item is not None  # nosec B101
        result = item.to_snapshot_shape()
        assert result is not None  # nosec B101
        self.assertEqual(result["type"], shape_type)
        for original, restored in zip(latlngs, result["latlngs"], strict=True):
            self.assertAlmostEqual(original[0], restored[0], places=9)
            self.assertAlmostEqual(original[1], restored[1], places=9)

    @settings(max_examples=25, deadline=None)
    @given(latlngs=st.lists(_latlng, min_size=3, max_size=8))
    def test_polygon_round_trip(self, latlngs: list) -> None:
        item = PinMarkup.from_snapshot_shape(_shape("polygon", latlngs))
        assert item is not None  # nosec B101
        result = item.to_snapshot_shape()
        assert result is not None  # nosec B101
        self.assertEqual(result["type"], "polygon")
        self.assertEqual(len(result["latlngs"]), len(latlngs))

    @settings(max_examples=25, deadline=None)
    @given(corner_a=_latlng, corner_b=_latlng)
    def test_rect_round_trip(self, corner_a: list, corner_b: list) -> None:
        item = PinMarkup.from_snapshot_shape(_shape("rect", [corner_a, corner_b]))
        assert item is not None  # nosec B101
        self.assertEqual(item.markup_type, "square")
        result = item.to_snapshot_shape()
        assert result is not None  # nosec B101
        self.assertEqual(result["type"], "rect")
        self.assertAlmostEqual(result["latlngs"][0][0], corner_a[0], places=9)
        self.assertAlmostEqual(result["latlngs"][0][1], corner_a[1], places=9)
        self.assertAlmostEqual(result["latlngs"][1][0], corner_b[0], places=9)
        self.assertAlmostEqual(result["latlngs"][1][1], corner_b[1], places=9)

    @settings(max_examples=25, deadline=None)
    @given(
        center=_latlng,
        # Hand-drawn circles are at most tens of km across; the two-point
        # client representation is a local (flat-earth) approximation, so
        # only radii in that realistic range are expected to round-trip.
        dlat=st.floats(min_value=-0.4, max_value=0.4, allow_nan=False),
        dlng=st.floats(min_value=-0.4, max_value=0.4, allow_nan=False),
    )
    def test_circle_radius_round_trips(self, center: list, dlat: float, dlng: float) -> None:
        edge = [center[0] + dlat, center[1] + dlng]
        item = PinMarkup.from_snapshot_shape(_shape("circle", [center, edge]))
        assert item is not None  # nosec B101
        radius = item.geometry["radius"]
        result = item.to_snapshot_shape()
        assert result is not None  # nosec B101
        restored = PinMarkup.from_snapshot_shape(result)
        assert restored is not None  # nosec B101
        # Radius survives conversion to an edge point and back within 1%.
        if radius > 1:
            self.assertAlmostEqual(restored.geometry["radius"] / radius, 1.0, delta=0.01)

    def test_text_with_box_corner_round_trips(self) -> None:
        item = PinMarkup.from_snapshot_shape(_shape("text", [[40.0, -74.0], [40.1, -73.9]], label="Entrance", stroke_width=18))
        assert item is not None  # nosec B101
        self.assertEqual(item.geometry["type"], "Point")
        self.assertEqual(item.geometry["coordinates"], [-74.0, 40.0])
        self.assertEqual(item.geometry["box_corner"], [-73.9, 40.1])
        result = item.to_snapshot_shape()
        assert result is not None  # nosec B101
        self.assertEqual(result["label"], "Entrance")
        self.assertEqual(result["latlngs"], [[40.0, -74.0], [40.1, -73.9]])

    def test_unknown_or_degenerate_shapes_return_none(self) -> None:
        self.assertIsNone(PinMarkup.from_snapshot_shape(_shape("line", [[0.0, 0.0]])))
        self.assertIsNone(PinMarkup.from_snapshot_shape(_shape("polygon", [[0.0, 0.0], [1.0, 1.0]])))
        self.assertIsNone(PinMarkup.from_snapshot_shape(_shape("__proto__", [[0.0, 0.0], [1.0, 1.0]])))

    def test_style_fields_survive(self) -> None:
        item = PinMarkup.from_snapshot_shape(_shape("line", [[0.0, 0.0], [1.0, 1.0]], border_color="none", label="path"))
        assert item is not None  # nosec B101
        result = item.to_snapshot_shape()
        assert result is not None  # nosec B101
        self.assertEqual(result["color"], "#2196f3")
        self.assertEqual(result["stroke_width"], 4)
        self.assertEqual(result["fill_opacity"], 60)
        self.assertEqual(result["border_opacity"], 90)
        self.assertEqual(result["border_color"], "none")
        self.assertEqual(result["label"], "path")


class SanitizeLayerFieldsTests(TestCase):
    """sanitize_map_data whitelists layer_mode and coerces show_borders."""

    def test_valid_layer_modes_pass(self) -> None:
        for mode in ("street", "satellite", "topographic", "dark"):
            result = sanitize_map_data({"center_lat": 0.0, "center_lng": 0.0, "layer_mode": mode})
            assert result is not None  # nosec B101
            self.assertEqual(result["layer_mode"], mode)

    def test_legacy_layer_modes_normalize_to_canonical(self) -> None:
        for legacy, canonical in (("standard", "street"), ("topo", "topographic")):
            result = sanitize_map_data({"center_lat": 0.0, "center_lng": 0.0, "layer_mode": legacy})
            assert result is not None  # nosec B101
            self.assertEqual(result["layer_mode"], canonical)

    def test_unknown_layer_mode_falls_back_to_street(self) -> None:
        result = sanitize_map_data({"center_lat": 0.0, "center_lng": 0.0, "layer_mode": "javascript:alert(1)"})
        assert result is not None  # nosec B101
        self.assertEqual(result["layer_mode"], "street")

    def test_show_borders_is_coerced_to_bool(self) -> None:
        result = sanitize_map_data({"center_lat": 0.0, "center_lng": 0.0, "show_borders": "yes"})
        assert result is not None  # nosec B101
        self.assertIs(result["show_borders"], True)
        result = sanitize_map_data({"center_lat": 0.0, "center_lng": 0.0})
        assert result is not None  # nosec B101
        self.assertIs(result["show_borders"], False)


def _snapshot(**overrides) -> dict:
    snapshot = {
        "center_lat": 40.5,
        "center_lng": -74.2,
        "zoom": 15.0,
        "layer_mode": "satellite",
        "show_borders": True,
        "markup": [
            _shape("line", [[40.5, -74.2], [40.6, -74.1]]),
            _shape("text", [[40.55, -74.15]], label="Gate"),
        ],
    }
    snapshot.update(overrides)
    return snapshot


class MarkupMapSnapshotTests(TestCase):
    """replace_items_from_snapshot and to_snapshot mirror each other."""

    def setUp(self) -> None:
        self.user = baker.make("auth.User")
        self.profile = self.user.profile

    def test_replace_items_builds_items_and_viewport(self) -> None:
        markup_map = MarkupMap.objects.create(profile=self.profile)
        markup_map.replace_items_from_snapshot(_snapshot())
        markup_map.refresh_from_db()
        self.assertEqual(markup_map.center_latitude, 40.5)
        self.assertEqual(markup_map.center_longitude, -74.2)
        self.assertEqual(markup_map.layer_mode, "satellite")
        self.assertTrue(markup_map.show_borders)
        self.assertEqual(markup_map.items.count(), 2)

    def test_to_snapshot_round_trips_viewport_and_shapes(self) -> None:
        markup_map = MarkupMap.objects.create(profile=self.profile)
        markup_map.replace_items_from_snapshot(_snapshot())
        result = markup_map.to_snapshot()
        self.assertEqual(result["center_lat"], 40.5)
        self.assertEqual(result["layer_mode"], "satellite")
        self.assertIs(result["show_borders"], True)
        self.assertEqual({s["type"] for s in result["markup"]}, {"line", "text"})

    def test_replace_items_drops_previous_items(self) -> None:
        markup_map = MarkupMap.objects.create(profile=self.profile)
        markup_map.replace_items_from_snapshot(_snapshot())
        markup_map.replace_items_from_snapshot(_snapshot(markup=[_shape("line", [[1.0, 1.0], [2.0, 2.0]])]))
        self.assertEqual(markup_map.items.count(), 1)


class DefaultMarkupMapTitleTests(TestCase):
    """default_markup_map_title() - date-only, pin-named, and wiki-named defaults."""

    def _today(self) -> str:
        return format_date(timezone.localdate(), "M j, Y")

    def test_no_context_returns_date_only(self) -> None:
        self.assertEqual(default_markup_map_title(), self._today())

    def test_pin_context_uses_pin_name_and_date(self) -> None:
        pin = baker.make("dashboard.Pin", name="Old Mill")
        self.assertEqual(default_markup_map_title(pin), f"Old Mill - {self._today()}")

    def test_wiki_context_uses_wiki_name_and_date(self) -> None:
        location = baker.make("dashboard.Location")
        wiki = baker.make("dashboard.Wiki", location=location, name="Curated Mill")
        self.assertEqual(default_markup_map_title(wiki), f"Curated Mill - {self._today()}")


class MaterializeMarkupMapTests(TestCase):
    """materialize_markup_map create/update/remove semantics."""

    def setUp(self) -> None:
        self.user = baker.make("auth.User")
        self.profile = self.user.profile

    def test_none_snapshot_and_no_existing_returns_none(self) -> None:
        self.assertIsNone(materialize_markup_map(self.profile, None))

    def test_creates_map_from_snapshot(self) -> None:
        markup_map = materialize_markup_map(self.profile, _snapshot())
        assert markup_map is not None  # nosec B101
        self.assertEqual(markup_map.profile_id, self.profile.pk)
        self.assertEqual(markup_map.items.count(), 2)

    def test_creates_map_with_date_only_default_title(self) -> None:
        markup_map = materialize_markup_map(self.profile, _snapshot())
        assert markup_map is not None  # nosec B101
        self.assertEqual(markup_map.title, format_date(timezone.localdate(), "M j, Y"))

    def test_updates_existing_map_in_place(self) -> None:
        existing = materialize_markup_map(self.profile, _snapshot())
        updated = materialize_markup_map(self.profile, _snapshot(markup=[]), existing_map=existing)
        assert updated is not None  # nosec B101
        self.assertEqual(updated.pk, existing.pk)
        self.assertEqual(updated.items.count(), 0)

    def test_none_snapshot_deletes_existing_map(self) -> None:
        existing = materialize_markup_map(self.profile, _snapshot())
        self.assertIsNone(materialize_markup_map(self.profile, None, existing_map=existing))
        self.assertFalse(MarkupMap.objects.filter(pk=existing.pk).exists())


class UnattachedQuerySetTests(TestCase):
    """unattached() excludes maps referenced by any host model."""

    def setUp(self) -> None:
        self.user = baker.make("auth.User")
        self.profile = self.user.profile

    def test_draft_map_is_unattached(self) -> None:
        markup_map = MarkupMap.objects.create(profile=self.profile)
        self.assertIn(markup_map, MarkupMap.objects.unattached())

    def test_checkin_linked_map_is_attached(self) -> None:
        markup_map = MarkupMap.objects.create(profile=self.profile)
        baker.make(
            "dashboard.SafetyCheckin",
            profile=self.profile,
            checkin_by=timezone.now() + datetime.timedelta(hours=3),
            markup_map=markup_map,
        )
        self.assertNotIn(markup_map, MarkupMap.objects.unattached())
        self.assertTrue(markup_map.is_attached)

    def test_visit_linked_map_is_attached(self) -> None:
        markup_map = MarkupMap.objects.create(profile=self.profile)
        pin = baker.make("dashboard.Pin", profile=self.profile)
        baker.make("dashboard.PinVisit", pin=pin, visited_at=timezone.now(), markup_map=markup_map)
        self.assertNotIn(markup_map, MarkupMap.objects.unattached())


class MarkupMapEndpointTests(TestCase):
    """The /markup-maps/ endpoints: create, items, view-state, delete, ownership."""

    def setUp(self) -> None:
        self.user = baker.make("auth.User")
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def _create_map(self) -> str:
        response = self.client.post(
            reverse("markup_map.create"),
            data=json.dumps({"center_lat": 40.0, "center_lng": -74.0, "zoom": 12, "layer_mode": "topographic", "show_borders": True}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["uuid"]

    def test_create_returns_uuid_and_applies_view(self) -> None:
        map_uuid = self._create_map()
        markup_map = MarkupMap.objects.get(uuid=map_uuid)
        self.assertEqual(markup_map.profile_id, self.profile.pk)
        self.assertEqual(markup_map.layer_mode, "topographic")
        self.assertTrue(markup_map.show_borders)
        self.assertEqual(markup_map.zoom, 12)

    def test_view_state_accepts_legacy_layer_mode_alias(self) -> None:
        map_uuid = self._create_map()
        response = self.client.post(
            reverse("markup_map.view_state", args=[map_uuid]),
            data=json.dumps({"layer_mode": "topo"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(MarkupMap.objects.get(uuid=map_uuid).layer_mode, "topographic")

    def test_item_create_and_json_listing(self) -> None:
        map_uuid = self._create_map()
        response = self.client.post(
            reverse("markup_map.markup", args=[map_uuid]),
            data=json.dumps({
                "markup_type": "line",
                "geometry": {"type": "LineString", "coordinates": [[-74.0, 40.0], [-73.9, 40.1]]},
                "color": "#123456",
            }),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        listing = self.client.get(reverse("markup_map.json", args=[map_uuid])).json()
        self.assertEqual(len(listing["markup_items"]), 1)
        self.assertEqual(listing["markup_items"][0]["color"], "#123456")
        self.assertEqual(listing["view"]["layer_mode"], "topographic")

    def test_create_with_markup_persists_snapshot_in_one_call(self) -> None:
        """The standalone "take a screenshot" flow: a full snapshot (viewport + shapes) in one POST."""
        response = self.client.post(
            reverse("markup_map.create"),
            data=json.dumps(_snapshot()),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        markup_map = MarkupMap.objects.get(uuid=response.json()["uuid"])
        self.assertEqual(markup_map.items.count(), 2)
        self.assertEqual(markup_map.layer_mode, "satellite")
        self.assertTrue(markup_map.show_borders)
        self.assertEqual(markup_map.title, format_date(timezone.localdate(), "M j, Y"))

    def test_create_with_invalid_markup_snapshot_returns_400(self) -> None:
        response = self.client.post(
            reverse("markup_map.create"),
            data=json.dumps({"markup": [], "center_lat": "not-a-number"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_create_with_explicit_title_overrides_default(self) -> None:
        response = self.client.post(
            reverse("markup_map.create"),
            data=json.dumps(_snapshot(title="My Custom Title")),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        markup_map = MarkupMap.objects.get(uuid=response.json()["uuid"])
        self.assertEqual(markup_map.title, "My Custom Title")

    def test_create_with_pin_slug_uses_pin_named_default_title(self) -> None:
        pin = baker.make("dashboard.Pin", profile=self.profile, name="Old Mill")
        response = self.client.post(
            reverse("markup_map.create"),
            data=json.dumps(_snapshot(pin_slug=pin.slug)),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        markup_map = MarkupMap.objects.get(uuid=response.json()["uuid"])
        self.assertEqual(markup_map.title, f"Old Mill - {format_date(timezone.localdate(), 'M j, Y')}")

    def test_create_with_location_slug_uses_wiki_named_default_title(self) -> None:
        location = baker.make("dashboard.Location")
        baker.make("dashboard.Wiki", location=location, name="Curated Mill")
        baker.make("dashboard.Pin", profile=self.profile, location=location)
        response = self.client.post(
            reverse("markup_map.create"),
            data=json.dumps(_snapshot(location_slug=location.slug)),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        markup_map = MarkupMap.objects.get(uuid=response.json()["uuid"])
        self.assertEqual(markup_map.title, f"Curated Mill - {format_date(timezone.localdate(), 'M j, Y')}")

    def test_create_with_another_users_pin_slug_falls_back_to_date_only(self) -> None:
        other_user = baker.make("auth.User")
        other_pin = baker.make("dashboard.Pin", profile=other_user.profile, name="Not Yours")
        response = self.client.post(
            reverse("markup_map.create"),
            data=json.dumps(_snapshot(pin_slug=other_pin.slug)),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        markup_map = MarkupMap.objects.get(uuid=response.json()["uuid"])
        self.assertEqual(markup_map.title, format_date(timezone.localdate(), "M j, Y"))

    def test_view_state_endpoint_updates_viewport(self) -> None:
        map_uuid = self._create_map()
        response = self.client.post(
            reverse("markup_map.view_state", args=[map_uuid]),
            data=json.dumps({"center_lat": 41.0, "center_lng": -75.0, "zoom": 99, "layer_mode": "satellite", "show_borders": False}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        markup_map = MarkupMap.objects.get(uuid=map_uuid)
        self.assertEqual(markup_map.center_latitude, 41.0)
        self.assertEqual(markup_map.layer_mode, "satellite")
        self.assertFalse(markup_map.show_borders)
        self.assertLessEqual(markup_map.zoom, 22)

    def test_delete_endpoint_removes_map_and_items(self) -> None:
        map_uuid = self._create_map()
        markup_map = MarkupMap.objects.get(uuid=map_uuid)
        baker.make(
            "dashboard.PinMarkup",
            parent_map=markup_map,
            profile=self.profile,
            markup_type="line",
            geometry={"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
        )
        response = self.client.post(reverse("markup_map.delete", args=[map_uuid]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(MarkupMap.objects.filter(uuid=map_uuid).exists())
        self.assertFalse(PinMarkup.objects.filter(parent_map__uuid=map_uuid).exists())

    def test_other_users_map_is_a_404(self) -> None:
        map_uuid = self._create_map()
        other = baker.make("auth.User")
        self.client.force_login(other)
        self.assertEqual(self.client.get(reverse("markup_map.json", args=[map_uuid])).status_code, 404)
        self.assertEqual(self.client.post(reverse("markup_map.delete", args=[map_uuid])).status_code, 404)

    def test_snapshot_endpoint_returns_to_snapshot_shape(self) -> None:
        map_uuid = self._create_map()
        baker.make(
            "dashboard.PinMarkup",
            parent_map=MarkupMap.objects.get(uuid=map_uuid),
            profile=self.profile,
            markup_type="line",
            geometry={"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
        )
        response = self.client.get(reverse("markup_map.snapshot", args=[map_uuid]))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["center_lat"], 40.0)
        self.assertEqual(data["layer_mode"], "topographic")
        self.assertEqual(len(data["markup"]), 1)
        self.assertEqual(data["markup"][0]["latlngs"], [[0, 0], [1, 1]])

    def test_snapshot_of_other_users_map_is_a_404(self) -> None:
        map_uuid = self._create_map()
        other = baker.make("auth.User")
        self.client.force_login(other)
        self.assertEqual(self.client.get(reverse("markup_map.snapshot", args=[map_uuid])).status_code, 404)


class CheckinCreateLinksMapTests(TestCase):
    """The check-in create POST links a draft map submitted in ``markup_map``."""

    def setUp(self) -> None:
        self.user = baker.make("auth.User")
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def _create_checkin(self, **extra) -> object:
        data = {
            "title": "Night hike",
            "checkin_by": (timezone.now() + datetime.timedelta(hours=5)).isoformat(),
            "grace_period_hours": "1",
            **extra,
        }
        response = self.client.post(reverse("safety.checkin.create"), data)
        self.assertEqual(response.status_code, 302)
        from urbanlens.dashboard.models.safety.model import SafetyCheckin

        return SafetyCheckin.objects.filter(profile=self.profile).latest("created")

    def test_draft_map_is_linked(self) -> None:
        markup_map = MarkupMap.objects.create(profile=self.profile)
        checkin = self._create_checkin(markup_map=str(markup_map.uuid))
        self.assertEqual(checkin.markup_map_id, markup_map.pk)

    def test_foreign_map_is_ignored(self) -> None:
        other = baker.make("auth.User")
        foreign_map = MarkupMap.objects.create(profile=other.profile)
        checkin = self._create_checkin(markup_map=str(foreign_map.uuid))
        self.assertIsNone(checkin.markup_map_id)

    def test_garbage_uuid_is_ignored(self) -> None:
        checkin = self._create_checkin(markup_map="not-a-uuid")
        self.assertIsNone(checkin.markup_map_id)


class DeleteFlagsAttachedContentTests(TestCase):
    """Deleting a MarkupMap flags every Comment/TripComment/DirectMessage that referenced it.

    ``on_delete=SET_NULL`` already detaches the map without touching the
    host row's text, but without a separate flag there'd be no way to tell
    "never had a map" from "had one that was deleted" once the FK is nulled.
    """

    def setUp(self) -> None:
        self.user = baker.make("auth.User")
        self.profile = self.user.profile

    def test_comment_is_flagged_and_text_survives(self) -> None:
        markup_map = MarkupMap.objects.create(profile=self.profile)
        pin = baker.make("dashboard.Pin", profile=self.profile)
        comment = baker.make("dashboard.Comment", pin=pin, profile=self.profile, markup_map=markup_map, text="Watch out here")
        markup_map.delete()
        comment.refresh_from_db()
        self.assertIsNone(comment.markup_map_id)
        self.assertTrue(comment.map_removed)
        self.assertEqual(comment.text, "Watch out here")

    def test_trip_comment_is_flagged(self) -> None:
        markup_map = MarkupMap.objects.create(profile=self.profile)
        trip = baker.make("dashboard.Trip", creator=self.profile)
        trip_comment = baker.make("dashboard.TripComment", trip=trip, author=self.profile, markup_map=markup_map, text="Route looks good")
        markup_map.delete()
        trip_comment.refresh_from_db()
        self.assertIsNone(trip_comment.markup_map_id)
        self.assertTrue(trip_comment.map_removed)
        self.assertEqual(trip_comment.text, "Route looks good")

    def test_direct_message_is_flagged(self) -> None:
        recipient_user = baker.make("auth.User")
        markup_map = MarkupMap.objects.create(profile=self.profile)
        message = baker.make(
            "dashboard.DirectMessage",
            sender=self.profile,
            recipient=recipient_user.profile,
            markup_map=markup_map,
            body="Check out this spot",
        )
        markup_map.delete()
        message.refresh_from_db()
        self.assertIsNone(message.markup_map_id)
        self.assertTrue(message.map_removed)
        self.assertEqual(message.body, "Check out this spot")

    def test_unrelated_comment_is_not_flagged(self) -> None:
        markup_map = MarkupMap.objects.create(profile=self.profile)
        pin = baker.make("dashboard.Pin", profile=self.profile)
        untouched = baker.make("dashboard.Comment", pin=pin, profile=self.profile, markup_map=None, text="No map here")
        markup_map.delete()
        untouched.refresh_from_db()
        self.assertFalse(untouched.map_removed)


class AttachmentsPropertyTests(TestCase):
    """MarkupMap.attachments enumerates every host referencing a map, including DMs."""

    def setUp(self) -> None:
        self.user = baker.make("auth.User")
        self.profile = self.user.profile

    def test_lists_every_attachment_kind_at_once(self) -> None:
        markup_map = MarkupMap.objects.create(profile=self.profile)
        pin = baker.make("dashboard.Pin", profile=self.profile)
        recipient_user = baker.make("auth.User")
        comment = baker.make("dashboard.Comment", pin=pin, profile=self.profile, markup_map=markup_map)
        message = baker.make("dashboard.DirectMessage", sender=self.profile, recipient=recipient_user.profile, markup_map=markup_map)

        kinds = {kind for kind, _host in markup_map.attachments}
        self.assertEqual(kinds, {"comment", "direct_message"})
        hosts = {host.pk for _kind, host in markup_map.attachments}
        self.assertEqual(hosts, {comment.pk, message.pk})

    def test_unattached_map_has_no_attachments(self) -> None:
        markup_map = MarkupMap.objects.create(profile=self.profile)
        self.assertEqual(markup_map.attachments, [])
