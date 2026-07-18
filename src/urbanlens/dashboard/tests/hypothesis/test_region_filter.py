"""Tests for geographic include/exclude region filtering.

Covers three layers:

- ``services.geo.dissolve_polygons`` - the merge-overlapping-polygons helper.
- ``Pin.objects.filter_by_criteria``'s ``include_regions``/``exclude_regions`` handling.
- ``services.filter_criteria``'s (de)serialization round-trip for regions.
"""
from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.contrib.gis.geos import MultiPolygon, Polygon
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_list.model import PinList
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.saved_filter.model import SavedFilter
from urbanlens.dashboard.services.filter_criteria import deserialize_criteria, serialize_form_criteria
from urbanlens.dashboard.services.geo import dissolve_polygons


def _square(lng: float, lat: float, delta: float) -> Polygon:
    ring = (
        (lng - delta, lat - delta),
        (lng + delta, lat - delta),
        (lng + delta, lat + delta),
        (lng - delta, lat + delta),
        (lng - delta, lat - delta),
    )
    return Polygon(ring, srid=4326)


class DissolvePolygonsTests(TestCase):
    """dissolve_polygons: merge overlapping/touching same-type polygons."""

    def test_empty_input_returns_empty_multipolygon(self) -> None:
        result = dissolve_polygons([])
        self.assertIsInstance(result, MultiPolygon)
        self.assertEqual(len(result), 0)

    def test_single_polygon_passes_through_unchanged(self) -> None:
        square = _square(-74.0, 40.0, 0.01)
        result = dissolve_polygons([square])
        self.assertEqual(len(result), 1)
        self.assertTrue(result.equals(MultiPolygon(square, srid=4326)) or result.covers(square))

    def test_disjoint_polygons_stay_separate(self) -> None:
        a = _square(-74.0, 40.0, 0.001)
        b = _square(-70.0, 45.0, 0.001)
        result = dissolve_polygons([a, b])
        self.assertEqual(len(result), 2)

    def test_overlapping_polygons_merge_into_one(self) -> None:
        a = _square(-74.0, 40.0, 0.01)
        b = _square(-74.005, 40.0, 0.01)  # overlaps a
        result = dissolve_polygons([a, b])
        self.assertEqual(len(result), 1)
        self.assertTrue(result.contains(a.centroid))
        self.assertTrue(result.contains(b.centroid))

    def test_chained_overlaps_fully_merge(self) -> None:
        """A overlaps B, B overlaps C, A does not overlap C - all three must still merge into one."""
        a = _square(-74.00, 40.0, 0.006)
        b = _square(-73.99, 40.0, 0.006)  # overlaps a
        c = _square(-73.98, 40.0, 0.006)  # overlaps b, not a
        self.assertFalse(a.intersects(c))
        result = dissolve_polygons([a, b, c])
        self.assertEqual(len(result), 1)

    def test_touching_polygons_merge(self) -> None:
        # Built from literal, bit-identical shared coordinates (-73.995) rather
        # than independently-computed center+delta arithmetic, which can drift
        # by float rounding and turn an intended shared edge into a hairline
        # gap or overlap.
        a = Polygon(((-74.005, 39.995), (-73.995, 39.995), (-73.995, 40.005), (-74.005, 40.005), (-74.005, 39.995)), srid=4326)
        b = Polygon(((-73.995, 39.995), (-73.985, 39.995), (-73.985, 40.005), (-73.995, 40.005), (-73.995, 39.995)), srid=4326)
        self.assertTrue(a.touches(b))
        result = dissolve_polygons([a, b])
        self.assertEqual(len(result), 1)


class FilterByCriteriaRegionTests(TestCase):
    """Pin.objects.filter_by_criteria: include_regions/exclude_regions."""

    profile: Profile

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile
        self.region = MultiPolygon(_square(-74.0, 40.0, 0.01), srid=4326)
        inside_location = baker.make(Location, latitude=40.0, longitude=-74.0)
        outside_location = baker.make(Location, latitude=10.0, longitude=10.0)
        self.inside_pin = baker.make(Pin, profile=self.profile, location=inside_location)
        self.outside_pin = baker.make(Pin, profile=self.profile, location=outside_location)

    def _base_qs(self):
        return Pin.objects.filter(profile=self.profile)

    def test_include_regions_keeps_only_pins_inside(self) -> None:
        result_ids = set(self._base_qs().filter_by_criteria({"include_regions": self.region}).values_list("pk", flat=True))
        self.assertIn(self.inside_pin.pk, result_ids)
        self.assertNotIn(self.outside_pin.pk, result_ids)

    def test_exclude_regions_drops_pins_inside(self) -> None:
        result_ids = set(self._base_qs().filter_by_criteria({"exclude_regions": self.region}).values_list("pk", flat=True))
        self.assertNotIn(self.inside_pin.pk, result_ids)
        self.assertIn(self.outside_pin.pk, result_ids)

    def test_omitting_regions_applies_no_geographic_filter(self) -> None:
        result_ids = set(self._base_qs().filter_by_criteria({}).values_list("pk", flat=True))
        self.assertIn(self.inside_pin.pk, result_ids)
        self.assertIn(self.outside_pin.pk, result_ids)

    def test_include_and_exclude_together_can_carve_a_hole(self) -> None:
        """A smaller exclude region inside a larger include region is a valid, meaningful configuration."""
        hole = MultiPolygon(_square(-74.0, 40.0, 0.001), srid=4326)
        another_inside_location = baker.make(Location, latitude=40.0005, longitude=-74.0005)
        pin_in_hole = baker.make(Pin, profile=self.profile, location=another_inside_location)
        # inside_pin (40.0, -74.0) sits inside the hole too (hole is centered the same) -
        # use a pin further out within the include region but outside the hole instead.
        near_edge_location = baker.make(Location, latitude=40.008, longitude=-74.008)
        pin_near_edge = baker.make(Pin, profile=self.profile, location=near_edge_location)

        result_ids = set(
            self._base_qs().filter_by_criteria({"include_regions": self.region, "exclude_regions": hole}).values_list("pk", flat=True),
        )
        self.assertNotIn(pin_in_hole.pk, result_ids)
        self.assertIn(pin_near_edge.pk, result_ids)


class FilterCriteriaRegionSerializationTests(TestCase):
    """serialize_form_criteria/deserialize_criteria round-trip for regions."""

    profile: Profile

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile

    def test_round_trip_preserves_region_geometry(self) -> None:
        region = MultiPolygon(_square(-74.0, 40.0, 0.01), srid=4326)
        stored = serialize_form_criteria({}, None, None, {"include_regions": region, "exclude_regions": None})
        self.assertIn("include_regions", stored)
        self.assertNotIn("exclude_regions", stored)

        criteria = deserialize_criteria(stored, self.profile)
        self.assertIsInstance(criteria["include_regions"], MultiPolygon)
        self.assertTrue(criteria["include_regions"].equals(region))

    def test_no_regions_produces_no_keys(self) -> None:
        stored = serialize_form_criteria({}, None, None, None)
        self.assertEqual(stored, {})

    def test_malformed_stored_region_is_dropped_not_raised(self) -> None:
        criteria = deserialize_criteria({"include_regions": {"type": "Point", "coordinates": [0, 0]}}, self.profile)
        self.assertNotIn("include_regions", criteria)


class FiltersTabViewRenderingTests(TestCase):
    """Smoke tests that the new Filters tab and region-search views actually render.

    Template-syntax checks alone (get_template) don't catch context bugs like
    a bad attribute lookup or a missing context var - these hit the real
    views end-to-end with a logged-in client.
    """

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.profile = self.user.profile

    def test_lists_page_filters_tab_renders(self) -> None:
        # /lists/ only serves the HTMX fragment for HX-Request; direct
        # navigation redirects to the equivalent Organize tab (see
        # PinListsIndexView's docstring).
        response = self.client.get(reverse("lists.list"), {"tab": "filters"}, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)

    def test_new_filter_dialog_renders(self) -> None:
        response = self.client.get(reverse("saved_filters.new"))
        self.assertEqual(response.status_code, 200)

    def test_edit_filter_dialog_renders_with_regions(self) -> None:
        region = MultiPolygon(_square(-74.0, 40.0, 0.01), srid=4326)
        saved_filter = SavedFilter.objects.create(
            profile=self.profile,
            name="Region filter",
            criteria={"min_rating": 3, "include_regions": {"type": "MultiPolygon", "coordinates": region.coords}},
        )
        response = self.client.get(reverse("saved_filters.edit", kwargs={"filter_uuid": saved_filter.uuid}))
        self.assertEqual(response.status_code, 200)

    def test_region_search_returns_polygonal_results_only(self) -> None:
        fake_results = [
            {"display_name": "Albany, NY, USA", "geojson": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}},
            {"display_name": "123 Main St", "geojson": {"type": "Point", "coordinates": [0, 0]}},
        ]
        with mock.patch("urbanlens.dashboard.controllers.region_search.NominatimGateway.search", return_value=fake_results):
            response = self.client.get(reverse("region_search.search"), {"q": "Albany, NY"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data["results"]), 1)
        self.assertEqual(data["results"][0]["display_name"], "Albany, NY, USA")

    def test_region_search_blank_query_returns_empty(self) -> None:
        response = self.client.get(reverse("region_search.search"), {"q": ""})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"results": []})

    def test_map_page_still_renders_with_new_hidden_region_fields(self) -> None:
        """map/index.html gained hidden include/exclude region inputs - confirm the page still renders."""
        response = self.client.get(reverse("map.view"))
        self.assertEqual(response.status_code, 200)


class SavedFilterLabelPickerTests(TestCase):
    """The Filters-tab include/exclude label pickers are a search-driven chip
    picker (see _saved_filter_label_picker.html + initSavedFilterLabelPickers in
    _saved_filter_dialog_scripts.html), reusing the same .apdlg-* markup/CSS as
    the main map's add-pin/bulk-edit label pickers. The server only renders a
    hidden data-id/data-selected catalog for the client-side JS to build chips
    and hidden checkboxes from - confirm that catalog carries the right state."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.profile = self.user.profile
        self.selected_label = Label.objects.create(profile=self.profile, name="Abandoned", kind="tag")
        self.other_label = Label.objects.create(profile=self.profile, name="Active", kind="tag")

    def test_new_filter_dialog_renders_all_labels_unselected(self) -> None:
        response = self.client.get(reverse("saved_filters.new"))
        html = response.content.decode()
        catalog_html = html.split('id="sf-label-catalog-tags"', 1)[1].split("</div>", 1)[0]
        self.assertIn(f'data-id="{self.selected_label.pk}"', catalog_html)
        self.assertIn(f'data-id="{self.other_label.pk}"', catalog_html)
        self.assertNotIn('data-selected="1"', catalog_html)

    def test_edit_filter_dialog_selects_only_the_saved_labels(self) -> None:
        saved_filter = SavedFilter.objects.create(
            profile=self.profile,
            name="Abandoned spots",
            criteria={"tags": [self.selected_label.pk]},
        )
        response = self.client.get(reverse("saved_filters.edit", kwargs={"filter_uuid": saved_filter.uuid}))
        html = response.content.decode()
        self.assertEqual(response.status_code, 200)

        # The selected label's catalog entry is marked selected; the other one's is not.
        selected_entry = html.split(f'data-id="{self.selected_label.pk}"', 1)[1][:120]
        other_entry = html.split(f'data-id="{self.other_label.pk}"', 1)[1][:120]
        self.assertIn('data-selected="1"', selected_entry)
        self.assertIn('data-selected="0"', other_entry)

        # Chip picker reuses the shared .apdlg-* component, not a bespoke checkbox-pill class.
        self.assertIn("saved-filter-label-picker", html)
        self.assertIn('id="sf-label-chips-tags"', html)


class PinListBoundaryDrawButtonTests(TestCase):
    """A pin list with no smart_boundary yet must expose a way to draw a first one,
    not just a "Clear" button that only appears once a boundary already exists."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.profile = self.user.profile

    def test_no_boundary_shows_draw_button_not_clear(self) -> None:
        pin_list = PinList.objects.create(profile=self.profile, name="No boundary yet")
        response = self.client.get(reverse("lists.detail", kwargs={"list_slug": pin_list.slug}))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        draw_btn = html.split('id="pin-list-boundary-draw-btn"', 1)[1][:80]
        clear_btn = html.split('id="pin-list-boundary-clear-btn"', 1)[1][:80]
        self.assertNotIn("hidden", draw_btn)
        self.assertIn("hidden", clear_btn)

    def test_existing_boundary_shows_clear_button_not_draw(self) -> None:
        boundary = MultiPolygon(_square(-74.0, 40.0, 0.01), srid=4326)
        pin_list = PinList.objects.create(profile=self.profile, name="Has boundary", smart_boundary=boundary)
        response = self.client.get(reverse("lists.detail", kwargs={"list_slug": pin_list.slug}))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        draw_btn = html.split('id="pin-list-boundary-draw-btn"', 1)[1][:80]
        clear_btn = html.split('id="pin-list-boundary-clear-btn"', 1)[1][:80]
        self.assertIn("hidden", draw_btn)
        self.assertNotIn("hidden", clear_btn)
