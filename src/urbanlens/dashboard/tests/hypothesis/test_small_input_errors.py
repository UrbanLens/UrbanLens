"""A user's own input that does not fit gets a refusal or a clamp, never a 500 (Spark P397)."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.controllers import maps, memories
from urbanlens.dashboard.models.saved_filter.model import SavedFilter
from urbanlens.dashboard.services.memories.aggregator import BBox

_NOT_A_BOX = ("nan,0,1,1", "0,nan,1,1", "0,0,inf,1", "0,0,1,-inf", "nan,nan,nan,nan", "inf,inf,inf,inf")


class MapBboxTests(SimpleTestCase):
    def test_a_finite_ordered_box_is_parsed(self) -> None:
        self.assertEqual(maps._parse_bbox("39,-75,41,-73"), (39.0, -75.0, 41.0, -73.0))

    def test_a_box_across_the_antimeridian_is_still_a_box(self) -> None:
        """Leaflet reports west > east when the viewport crosses the date line; ``within_bounds`` splits it."""
        self.assertEqual(maps._parse_bbox("-10,170,10,-170"), (-10.0, 170.0, 10.0, -170.0))

    def test_non_finite_values_are_not_a_box(self) -> None:
        for raw in _NOT_A_BOX:
            with self.subTest(raw=raw):
                self.assertIsNone(maps._parse_bbox(raw))

    def test_a_south_edge_above_the_north_edge_is_not_a_box(self) -> None:
        self.assertIsNone(maps._parse_bbox("41,-75,39,-73"))


class MemoriesBboxTests(SimpleTestCase):
    def _request(self, bbox: str):
        from django.test import RequestFactory

        return RequestFactory().get("/memories/data/", {"bbox": bbox})

    def test_a_finite_ordered_box_is_parsed(self) -> None:
        self.assertEqual(memories._parse_bbox(self._request("39,-75,41,-73")), BBox(39.0, -75.0, 41.0, -73.0))

    def test_non_finite_values_are_not_a_box(self) -> None:
        for raw in _NOT_A_BOX:
            with self.subTest(raw=raw):
                self.assertIsNone(memories._parse_bbox(self._request(raw)))

    def test_a_south_edge_above_the_north_edge_is_not_a_box(self) -> None:
        self.assertIsNone(memories._parse_bbox(self._request("41,-75,39,-73")))

    def test_a_west_edge_past_the_east_edge_is_left_as_it_was(self) -> None:
        """A date-line viewport is not made unfiltered by this fix; only non-finite and south-above-north are refused."""
        self.assertEqual(memories._parse_bbox(self._request("-10,170,10,-170")), BBox(-10.0, 170.0, 10.0, -170.0))


class ViewportParametersDoNotCrashTheEndpointsTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.client.force_login(self.user)

    def test_the_memories_feed_survives_a_non_finite_viewport(self) -> None:
        for raw in _NOT_A_BOX:
            with self.subTest(raw=raw):
                self.assertEqual(self.client.get(reverse("memories.data"), {"bbox": raw}).status_code, 200)

    def test_the_map_pin_endpoints_survive_a_non_finite_viewport(self) -> None:
        for raw in _NOT_A_BOX:
            with self.subTest(raw=raw):
                self.assertEqual(self.client.get(reverse("map.pins"), {"bbox": raw}).status_code, 200)
                self.assertEqual(self.client.get(reverse("map.pins.list"), {"bounds": raw}).status_code, 200)


class SavedFilterEditInputTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.saved_filter = SavedFilter.objects.create(
            profile=self.user.profile, name="Mills", icon="factory", criteria={"name": "Mill"}
        )
        self.url = reverse("saved_filters.edit", kwargs={"filter_uuid": self.saved_filter.uuid})

    def test_an_over_long_icon_falls_back_to_the_default_instead_of_overflowing_the_column(self) -> None:
        response = self.client.post(self.url, {"filter_name": "Mills", "name": "Mill", "icon": "x" * 65})

        self.assertEqual(response.status_code, 200)
        self.saved_filter.refresh_from_db()
        self.assertEqual(self.saved_filter.icon, "bookmark")

    def test_an_icon_that_is_not_a_known_shape_is_not_stored_verbatim(self) -> None:
        self.client.post(self.url, {"filter_name": "Mills", "name": "Mill", "icon": "<script>alert(1)</script>"})

        self.saved_filter.refresh_from_db()
        self.assertEqual(self.saved_filter.icon, "bookmark")

    def test_a_known_icon_is_kept(self) -> None:
        self.client.post(self.url, {"filter_name": "Mills", "name": "Mill", "icon": "factory"})

        self.saved_filter.refresh_from_db()
        self.assertEqual(self.saved_filter.icon, "factory")

    def test_an_over_long_name_is_refused_as_the_create_view_refuses_it(self) -> None:
        response = self.client.post(self.url, {"filter_name": "n" * 101, "name": "Mill"})

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["ok"])
        self.saved_filter.refresh_from_db()
        self.assertEqual(self.saved_filter.name, "Mills")


class ProfileNameAutosaveTests(TestCase):
    def test_an_over_long_name_is_cut_to_the_column_not_a_500(self) -> None:
        baker.make(User)
        user = baker.make(User)
        self.client.force_login(user)

        for field in ("first_name", "last_name"):
            with self.subTest(field=field):
                response = self.client.post(reverse("profile.field.update"), {"field": field, "value": "n" * 200})

                self.assertEqual(response.status_code, 200)
                user.refresh_from_db()
                self.assertEqual(getattr(user, field), "n" * 150)
