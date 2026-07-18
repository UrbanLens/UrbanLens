"""Tests for the saved filter's own dedicated page: a live-updating map of
matching pins plus every editable option, replacing the old card "Edit"
dialog - see SavedFilterDetailView/SavedFilterPreviewView and
_filters_tab_grid.html's card link.
"""

from __future__ import annotations

import json

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.saved_filter.model import SavedFilter

_coord_counter = 0


def _make_pin(profile, **kwargs) -> Pin:
    """Create a pin with a real coordinate-bearing Location (unique per call)."""
    global _coord_counter
    location = kwargs.pop("location", None)
    if location is None:
        _coord_counter += 1
        location = baker.make(Location, latitude=40.0 + _coord_counter * 0.001, longitude=-74.0 - _coord_counter * 0.001)
    return baker.make(Pin, profile=profile, location=location, **kwargs)


class SavedFilterDetailViewTests(TestCase):
    """GET /saved-filters/<uuid>/ - the filter's own page."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.profile = self.user.profile
        self.matching = _make_pin(self.profile, name="Old Mill")
        self.non_matching = _make_pin(self.profile, name="New Factory")
        self.saved_filter = SavedFilter.objects.create(profile=self.profile, name="Mills", icon="filter_alt", criteria={"name": "Mill"})

    def test_renders_the_filter_name_and_every_field_group(self) -> None:
        response = self.client.get(reverse("saved_filters.detail", args=[self.saved_filter.uuid]))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("Mills", content)
        # Spot-check a representative field from each group added across the
        # filters-expansion batch, proving _saved_filter_fields.html is really
        # included (not just the dialog's old, narrower field set).
        self.assertIn('name="min_rating"', content)
        self.assertIn('name="date_built_after"', content)
        self.assertIn('name="has_links"', content)
        self.assertIn('name="min_detail_pins"', content)
        self.assertIn('name="security_fences"', content)
        self.assertIn('name="overlapping_pins"', content)

    def test_initial_pins_json_contains_only_matching_pins(self) -> None:
        response = self.client.get(reverse("saved_filters.detail", args=[self.saved_filter.uuid]))
        content = response.content.decode()
        payload = content.split('id="saved-filter-initial-pins"')[1].split("</script>")[0].split(">", 1)[1]
        pins = json.loads(payload)
        uuids = {row["uuid"] for row in pins}
        self.assertIn(str(self.matching.uuid), uuids)
        self.assertNotIn(str(self.non_matching.uuid), uuids)

    def test_404_for_another_users_filter(self) -> None:
        other_filter = SavedFilter.objects.create(profile=baker.make(User).profile, name="Not Mine", criteria={})
        response = self.client.get(reverse("saved_filters.detail", args=[other_filter.uuid]))
        self.assertEqual(response.status_code, 404)

    def test_404_for_a_nonexistent_filter(self) -> None:
        import uuid as uuid_module

        response = self.client.get(reverse("saved_filters.detail", args=[uuid_module.uuid4()]))
        self.assertEqual(response.status_code, 404)

    def test_save_action_points_at_the_existing_edit_endpoint(self) -> None:
        """The page's form reuses saved_filters.edit for Save - no new save
        endpoint was introduced, so resync/undo/derived-list logic there
        (already covered elsewhere) applies unchanged."""
        response = self.client.get(reverse("saved_filters.detail", args=[self.saved_filter.uuid]))
        self.assertContains(response, reverse("saved_filters.edit", args=[self.saved_filter.uuid]))


class SavedFilterPreviewViewTests(TestCase):
    """POST /saved-filters/preview/ - the live map's data source."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.profile = self.user.profile
        self.matching = _make_pin(self.profile, name="Old Mill")
        self.non_matching = _make_pin(self.profile, name="New Factory")

    def test_returns_only_matching_pins(self) -> None:
        response = self.client.post(reverse("saved_filters.preview"), {"name": "Mill"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        uuids = {row["uuid"] for row in data["pins"]}
        self.assertIn(str(self.matching.uuid), uuids)
        self.assertNotIn(str(self.non_matching.uuid), uuids)
        self.assertEqual(data["count"], 1)

    def test_empty_criteria_returns_every_pin(self) -> None:
        response = self.client.post(reverse("saved_filters.preview"), {})
        data = response.json()
        self.assertEqual(data["count"], 2)

    def test_never_returns_another_users_pins(self) -> None:
        other_profile = baker.make(User).profile
        _make_pin(other_profile, name="Someone Else's Mill")
        response = self.client.post(reverse("saved_filters.preview"), {"name": "Mill"})
        data = response.json()
        self.assertEqual(len(data["pins"]), 1)

    def test_invalid_security_choice_is_a_400_not_a_500(self) -> None:
        response = self.client.post(reverse("saved_filters.preview"), {"security_fences": "not_a_real_level"})
        self.assertEqual(response.status_code, 400)

    def test_every_returned_pin_carries_map_coordinates(self) -> None:
        response = self.client.post(reverse("saved_filters.preview"), {})
        data = response.json()
        self.assertTrue(data["pins"])
        for row in data["pins"]:
            self.assertIsInstance(row["lat"], float)
            self.assertIsInstance(row["lng"], float)


class FiltersTabCardLinksToDetailPageTests(TestCase):
    """The Filters tab's card: no more inline Edit button, whole card links to the new page."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.profile = self.user.profile
        self.saved_filter = SavedFilter.objects.create(profile=self.profile, name="Mills", criteria={"name": "Mill"})

    def _get_filters_tab(self):
        return self.client.get(reverse("lists.list"), {"tab": "filters"}, HTTP_HX_REQUEST="true")

    def test_card_links_to_the_detail_page(self) -> None:
        response = self._get_filters_tab()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("saved_filters.detail", args=[self.saved_filter.uuid]))

    def test_no_inline_edit_button(self) -> None:
        """The card's own Edit button (and its dialog-opening hx-on wiring) is
        gone - "New Filter" legitimately still opens the dialog for creating a
        new filter, so this only checks the edit-specific URL/wiring is absent."""
        response = self._get_filters_tab()
        self.assertNotContains(response, reverse("saved_filters.edit", args=[self.saved_filter.uuid]))
        content = response.content.decode()
        card_html = content.split('class="pin-list-card saved-filter-card"')[1].split("</a>")[0]
        self.assertNotIn("savedFilterOpenDialog", card_html)
        self.assertNotIn(">Edit<", card_html)

    def test_delete_button_still_present_and_stops_propagation(self) -> None:
        response = self._get_filters_tab()
        content = response.content.decode()
        self.assertIn("savedFilterDelete(", content)
        self.assertIn("saved-filter-delete-btn", content)
        self.assertIn("stopPropagation", content)
