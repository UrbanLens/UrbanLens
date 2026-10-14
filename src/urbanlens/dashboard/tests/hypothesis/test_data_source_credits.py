"""The home and credits pages list only the data sources a plugin contributes, grouped under the categories that have one."""

from __future__ import annotations

from unittest.mock import patch

from django.test import SimpleTestCase
from django.urls import reverse

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.plugins.extensions import DEFAULT_PRIORITY
from urbanlens.dashboard.plugins.points import (
    DATA_SOURCE_CATEGORIES,
    DATA_SOURCE_CREDITS,
    SLOT_THANKS_ATTRIBUTIONS,
    DataSourceCategory,
    DataSourceCredit,
    MapAttribution,
)
from urbanlens.dashboard.services.credits import credited_categories, credits_page_categories, map_cards

_TILES = MapAttribution(
    icon="map",
    text="Tiles © Example, subject to their",
    terms_label="terms of use",
    terms_url="https://tiles.example/terms",
)


def _core_categories() -> list[tuple[str, DataSourceCategory]]:
    """Core's own registrations, so the copy under test is core's whichever plugins are installed."""
    return [contribution.value for contribution in DATA_SOURCE_CATEGORIES.contributions() if contribution.owner is None]


class CreditedCategoriesTests(SimpleTestCase):
    def test_no_credit_means_no_categories(self) -> None:
        with DATA_SOURCE_CREDITS.overridden([]):
            self.assertEqual(credited_categories(), [])

    def test_only_categories_with_a_source_are_returned_in_category_order(self) -> None:
        contributed = [
            DataSourceCredit(name="Example Planning", category="planning"),
            DataSourceCredit(name="Example Archive", category="archives"),
            DataSourceCredit(name="Example Second Archive", category="archives"),
        ]
        with DATA_SOURCE_CREDITS.overridden(contributed):
            groups = credited_categories()

        self.assertEqual([group.category.title for group in groups], ["Historical archives", "Planning context"])
        self.assertEqual([credit.name for credit in groups[0].credits], ["Example Archive", "Example Second Archive"])

    def test_a_source_under_an_unknown_category_is_not_listed(self) -> None:
        with DATA_SOURCE_CREDITS.overridden([DataSourceCredit(name="Stray", category="no_such_category")]):
            self.assertEqual(credited_categories(), [])

    def test_every_core_category_has_its_copy(self) -> None:
        for key, category in _core_categories():
            with self.subTest(key=key):
                self.assertTrue(category.title and category.icon and category.description)

    def test_a_category_registered_at_a_lower_priority_replaces_core_copy(self) -> None:
        core = dict(_core_categories())["search"]
        replacement = DataSourceCategory(
            title="Search & more", icon="travel_explore", description="Search, and more.", order=core.order
        )
        with (
            DATA_SOURCE_CATEGORIES.overridden(_core_categories()),
            DATA_SOURCE_CREDITS.overridden([DataSourceCredit(name="X", category="search")]),
        ):
            DATA_SOURCE_CATEGORIES.add("search", replacement, priority=DEFAULT_PRIORITY - 10)
            (group,) = credited_categories()

        self.assertEqual(group.category, replacement)

    def test_core_copy_names_only_what_core_can_supply(self) -> None:
        copy = " ".join(category.title + category.description for _key, category in _core_categories()).lower()
        for word in ("property", "seismic", "park service", "wildlife", "wikipedia", "google", "weather"):
            self.assertNotIn(word, copy)

    def test_sources_of_one_category_are_listed_in_contribution_order(self) -> None:
        contributed = [
            DataSourceCredit(name="Second Imagery", category="places"),
            DataSourceCredit(name="First Imagery", category="places"),
        ]
        with DATA_SOURCE_CREDITS.overridden(contributed):
            (group,) = credited_categories()

        self.assertEqual([credit.name for credit in group.credits], ["Second Imagery", "First Imagery"])

    def test_map_cards_are_listed_on_the_home_page_and_left_out_of_the_credits_list(self) -> None:
        contributed = [
            DataSourceCredit(name="Tiles", category="open_map", attribution=_TILES),
            DataSourceCredit(name="Geocoder", category="open_map"),
        ]
        with DATA_SOURCE_CREDITS.overridden(contributed):
            home = [credit.name for group in credited_categories() for credit in group.credits]
            listed = [credit.name for group in credits_page_categories() for credit in group.credits]
            cards = [card.name for card in map_cards()]

        self.assertEqual(home, ["Tiles", "Geocoder"])
        self.assertEqual(listed, ["Geocoder"])
        self.assertEqual(cards, ["Tiles"])

    def test_a_category_whose_only_sources_are_map_cards_is_dropped_from_the_credits_list(self) -> None:
        with DATA_SOURCE_CREDITS.overridden([DataSourceCredit(name="Tiles", category="open_map", attribution=_TILES)]):
            self.assertEqual(credits_page_categories(), [])


class DataSourceCreditPagesTests(TestCase):
    def test_home_page_has_no_integrations_section_without_a_contribution(self) -> None:
        with DATA_SOURCE_CREDITS.overridden([]):
            response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Research every location in depth")
        self.assertNotContains(response, "home-apis")

    def test_home_page_shows_only_categories_with_a_credit(self) -> None:
        with DATA_SOURCE_CREDITS.overridden([DataSourceCredit(name="Example Archive", category="archives")]):
            response = self.client.get(reverse("home"))

        self.assertContains(response, "Research every location in depth")
        self.assertContains(response, "Historical archives")
        self.assertContains(response, "<li>Example Archive</li>", html=True)
        self.assertNotContains(response, "Planning context")

    def test_home_page_lists_a_map_card_source_by_its_name(self) -> None:
        with DATA_SOURCE_CREDITS.overridden(
            [DataSourceCredit(name="Example Tiles", category="open_map", attribution=_TILES)]
        ):
            response = self.client.get(reverse("home"))

        self.assertContains(response, "<li>Example Tiles</li>", html=True)

    def test_thanks_page_credits_contributed_sources_with_their_category_link_and_note(self) -> None:
        credit = DataSourceCredit(
            name="Example Archive",
            category="archives",
            url="https://archive.example/",
            note="Scanned maps of the area.",
        )
        with (
            DATA_SOURCE_CREDITS.overridden([credit]),
            SLOT_THANKS_ATTRIBUTIONS.overridden([]),
            patch("urbanlens.dashboard.controllers.thanks.get_github_contributors", return_value=[]),
        ):
            response = self.client.get(reverse("thanks"))

        self.assertContains(response, "Historical archives")
        self.assertContains(response, 'href="https://archive.example/"')
        self.assertContains(response, "Example Archive")
        self.assertContains(response, "Scanned maps of the area.")

    def test_thanks_page_shows_a_map_card_with_its_attribution_and_terms_link(self) -> None:
        credit = DataSourceCredit(
            name="Example Tiles", category="open_map", note="Tiles for the map.", attribution=_TILES
        )
        with (
            DATA_SOURCE_CREDITS.overridden([credit]),
            SLOT_THANKS_ATTRIBUTIONS.overridden([]),
            patch("urbanlens.dashboard.controllers.thanks.get_github_contributors", return_value=[]),
        ):
            response = self.client.get(reverse("thanks"))

        self.assertContains(response, "Tiles © Example, subject to their")
        self.assertContains(
            response,
            '<a href="https://tiles.example/terms" rel="noopener noreferrer" target="_blank">terms of use</a>',
            html=True,
        )

    def test_thanks_page_has_no_external_data_section_without_a_contribution(self) -> None:
        with (
            DATA_SOURCE_CREDITS.overridden([]),
            SLOT_THANKS_ATTRIBUTIONS.overridden([]),
            patch("urbanlens.dashboard.controllers.thanks.get_github_contributors", return_value=[]),
        ):
            response = self.client.get(reverse("thanks"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Enriched location information")
        self.assertContains(response, "Leaflet")
