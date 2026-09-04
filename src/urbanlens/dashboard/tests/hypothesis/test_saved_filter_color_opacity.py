"""Tests for saved filter color/opacity: create/edit persistence, validation,
and that the accent tint actually renders on the toolbar/sidebar/grid buttons.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.saved_filter.model import SavedFilter

#: The filter these tests create. A new profile now starts with two default
#: saved filters (see labels.signals.create_default_saved_filters), so a bare
#: ``objects.get(profile=...)`` no longer identifies "the one under test".
_FILTER_NAME = "Ruins"


#: The filter these tests create. A new profile now starts with two default
#: saved filters (see labels.signals.create_default_saved_filters), so a bare
#: ``objects.get(profile=...)`` no longer identifies "the one under test".
_FILTER_NAME = "Ruins"


class SavedFilterCreateColorOpacityTests(TestCase):
    def setUp(self) -> None:
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.profile = self.user.profile

    def _create(self, **extra):
        return self.client.post(reverse("saved_filters.create"), {"filter_name": _FILTER_NAME, "name": "Mill", **extra})

    def test_create_persists_color_and_opacity(self) -> None:
        response = self._create(color="#F44336", opacity="60")
        self.assertEqual(response.status_code, 200)
        saved_filter = SavedFilter.objects.get(profile=self.profile, name=_FILTER_NAME)
        self.assertEqual(saved_filter.color, "#F44336")
        self.assertEqual(saved_filter.opacity, 60)

    def test_create_without_color_or_opacity_defaults_to_blank_and_full(self) -> None:
        response = self._create()
        self.assertEqual(response.status_code, 200)
        saved_filter = SavedFilter.objects.get(profile=self.profile, name=_FILTER_NAME)
        self.assertEqual(saved_filter.color, "")
        self.assertEqual(saved_filter.opacity, 100)

    def test_create_with_invalid_color_blanks_it(self) -> None:
        response = self._create(color="javascript:alert(1)")
        self.assertEqual(response.status_code, 200)
        saved_filter = SavedFilter.objects.get(profile=self.profile, name=_FILTER_NAME)
        self.assertEqual(saved_filter.color, "")

    def test_create_clamps_out_of_range_opacity(self) -> None:
        response = self._create(opacity="500")
        self.assertEqual(response.status_code, 200)
        saved_filter = SavedFilter.objects.get(profile=self.profile, name=_FILTER_NAME)
        self.assertEqual(saved_filter.opacity, 100)


class SavedFilterEditColorOpacityTests(TestCase):
    def setUp(self) -> None:
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.profile = self.user.profile
        self.saved_filter = SavedFilter.objects.create(profile=self.profile, name="Mills", criteria={"name": "Mill"})

    def test_edit_updates_color_and_opacity(self) -> None:
        response = self.client.post(
            reverse("saved_filters.edit", kwargs={"filter_uuid": self.saved_filter.uuid}),
            {"filter_name": "Mills", "name": "Mill", "color": "#4CAF50", "opacity": "40"},
        )
        self.assertEqual(response.json()["ok"], True)
        self.saved_filter.refresh_from_db()
        self.assertEqual(self.saved_filter.color, "#4CAF50")
        self.assertEqual(self.saved_filter.opacity, 40)

    def test_edit_can_clear_color_back_to_blank(self) -> None:
        self.saved_filter.color = "#4CAF50"
        self.saved_filter.save(update_fields=["color"])
        response = self.client.post(
            reverse("saved_filters.edit", kwargs={"filter_uuid": self.saved_filter.uuid}),
            {"filter_name": "Mills", "name": "Mill", "color": ""},
        )
        self.assertEqual(response.json()["ok"], True)
        self.saved_filter.refresh_from_db()
        self.assertEqual(self.saved_filter.color, "")


class SavedFilterColorRendersOnButtonsTests(TestCase):
    """The color+opacity tint must actually show up wherever a saved filter's button renders."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.profile = self.user.profile
        SavedFilter.objects.create(
            profile=self.profile,
            name="Mills",
            icon="filter_alt",
            color="#F44336",
            opacity=50,
            criteria={"name": "Mill"},
        )

    def test_toolbar_button_carries_the_tint_and_colored_modifier(self) -> None:
        response = self.client.get(reverse("map.view"))
        content = response.content.decode()
        self.assertIn("sf-toolbar-btn--colored", content)
        self.assertIn("background:rgba(244,67,54,0.5)", content)

    def test_sidebar_chip_carries_the_tint(self) -> None:
        response = self.client.get(reverse("map.view"))
        content = response.content.decode()
        self.assertIn("fp-saved-filter-apply", content)
        self.assertIn("background:rgba(244,67,54,0.5)", content)

    def test_filters_grid_card_carries_the_tint(self) -> None:
        # Filters tab content is lazy-loaded via HTMX (see PinListsIndexView),
        # not baked into the initial organize.index page render.
        response = self.client.get(reverse("lists.list"), {"tab": "filters"}, HTTP_HX_REQUEST="true")
        content = response.content.decode()
        self.assertIn("background:rgba(244,67,54,0.5)", content)
