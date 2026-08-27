"""Creating a saved filter from the main map.

Two reported defects, one cause each:

- The map's Save Filter dialog offered only a name, though the create view has
  always accepted an icon, colour and opacity. The full create/edit dialog
  offered all of them; the map's copy had drifted, so the appearance fields
  now live in one shared partial that both include.
- A newly created filter did not appear until the page was reloaded. An
  out-of-band swap needs an element with that id *already in the DOM*, and the
  toolbar was rendered only `{% if saved_filters %}` - so a user with no
  filters had no `#map-saved-filters-toolbar` for the response to swap into,
  and htmx dropped the fragment.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.template.loader import render_to_string
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.meta import COLOR_CHOICES
from urbanlens.dashboard.models.saved_filter.model import SavedFilter

_TOOLBAR = "dashboard/partials/map/_saved_filters_toolbar.html"


class SavedFilterToolbarTargetTests(TestCase):
    """The OOB target has to exist before there is anything to put in it."""

    def test_the_toolbar_element_exists_even_with_no_filters(self) -> None:
        markup = render_to_string(_TOOLBAR, {"saved_filters": []})

        self.assertIn('id="map-saved-filters-toolbar"', markup, "with no target in the DOM, creating the first filter updates nothing until a reload")

    def test_the_empty_toolbar_is_invisible(self) -> None:
        """It must not draw empty toolbar chrome on a map with no filters."""
        markup = render_to_string(_TOOLBAR, {"saved_filters": []})

        self.assertNotIn("map-buttons", markup)

    def test_the_oob_flag_still_marks_it_for_swapping(self) -> None:
        markup = render_to_string(_TOOLBAR, {"saved_filters": [], "oob": True})

        self.assertIn('hx-swap-oob="true"', markup)


class MapSavedFilterCreateTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.client.force_login(self.user)

    def test_the_map_dialog_offers_icon_and_colour(self) -> None:
        response = self.client.get(reverse("map.view"))

        content = response.content.decode()
        self.assertIn('name="filter_name"', content)
        self.assertIn('name="color"', content, "the map dialog offered a name and nothing else")
        self.assertIn('name="opacity"', content)

    def test_creating_from_the_map_stores_the_chosen_appearance(self) -> None:
        colour = COLOR_CHOICES[0][0]

        self.client.post(
            reverse("saved_filters.create"),
            {"filter_name": "Demolition watch", "icon": "flag", "color": colour, "opacity": "80", "min_priority": "4"},
        )

        saved = SavedFilter.objects.get(profile=self.user.profile, name="Demolition watch")
        self.assertEqual(saved.icon, "flag")
        self.assertEqual(saved.color, colour)
        self.assertEqual(saved.opacity, 80)

    def test_the_create_response_carries_the_toolbar_for_the_map(self) -> None:
        """The map page swaps the section and takes the toolbar out of band."""
        response = self.client.post(
            reverse("saved_filters.create"),
            {"filter_name": "High priority", "min_priority": "4"},
        )

        content = response.content.decode()
        self.assertIn('id="map-saved-filters-toolbar"', content)
        self.assertIn('hx-swap-oob="true"', content)
