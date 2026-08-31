"""Tests for _external_tag_chips.html - the read-only external-tag chip partial.

Mirrors test_wiki_about_card.py's render_to_string approach. The negative
assertions (no remove button, no HTMX form) guard against ever accidentally
merging in _label_chips.html's editable membership-widget behavior - this
partial renders provider data a user cannot edit.
"""

from __future__ import annotations

from django.template.loader import render_to_string
from django.test import TestCase
from model_bakery import baker

from urbanlens.dashboard.models.place.external_tag import ExternalTagSource, PlaceExternalTag
from urbanlens.dashboard.models.place.model import Place

_TEMPLATE = "dashboard/partials/wiki/_external_tag_chips.html"


def _render(tags) -> str:
    return render_to_string(_TEMPLATE, {"tags": tags})


class EmptyTagsRenderNothingTests(TestCase):
    def test_no_card_renders_for_an_empty_queryset(self):
        place = baker.make(Place)

        html = _render(place.external_tags.all())

        self.assertNotIn("wiki-external-tags-card", html)
        self.assertNotIn("tag-chip", html)


class PopulatedTagsRenderChipsTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.place = baker.make(Place)
        PlaceExternalTag.objects.create(place=self.place, source=ExternalTagSource.OSM, key="amenity", value="restaurant", is_primary=True)
        PlaceExternalTag.objects.create(place=self.place, source=ExternalTagSource.OVERTURE, key="building_subtype", value="single_family_residential", is_primary=True)

    def test_the_card_renders(self) -> None:
        html = _render(self.place.external_tags.all())
        self.assertIn('id="wiki-external-tags-card"', html)

    def test_each_tag_renders_as_a_chip(self) -> None:
        html = _render(self.place.external_tags.all())

        self.assertEqual(html.count("tag-chip-name"), 2)
        self.assertIn("restaurant", html)
        self.assertIn("single family residential", html)  # humanized (underscores -> spaces)

    def test_no_remove_button_or_membership_form(self) -> None:
        html = _render(self.place.external_tags.all())

        self.assertNotIn("tag-chip-remove", html)
        self.assertNotIn("hx-post", html)
        self.assertNotIn("<form", html)

    def test_source_specific_modifier_class_is_present(self) -> None:
        html = _render(self.place.external_tags.all())

        self.assertIn("tag-chip--osm", html)
        self.assertIn("tag-chip--overture", html)
