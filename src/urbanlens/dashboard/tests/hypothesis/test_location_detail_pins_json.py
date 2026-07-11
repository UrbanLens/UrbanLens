"""Tests for LocationDetailPinJsonView - a wiki's child wikis as map markers."""

from __future__ import annotations

import json

from django.contrib.auth.models import User
from django.test import RequestFactory
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.detail_pins import LocationDetailPinJsonView
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.wiki.model import Wiki


def _make_location() -> Location:
    loc = baker.make(Location)
    if not loc.slug:
        Location.objects.filter(pk=loc.pk).update(slug=f"loc-{loc.pk}")
        loc.refresh_from_db()
    return loc


def _wiki_for(location: Location) -> Wiki:
    wiki, _ = Wiki.objects.get_or_create_for_location(location)
    return wiki


def _make_child_wiki(parent: Wiki, **kwargs) -> Wiki:
    child_location = baker.make(Location)
    return baker.make(Wiki, location=child_location, parent_wiki=parent, **kwargs)


class LocationDetailPinJsonChildWikiTests(TestCase):
    """The endpoint returns the wiki's child wikis, not its own root entry."""

    def setUp(self) -> None:
        self.factory = RequestFactory()
        self.location = _make_location()
        self.wiki = _wiki_for(self.location)
        self.viewer = baker.make(User)
        self.child_a = _make_child_wiki(self.wiki, name="North Entrance")
        self.child_b = _make_child_wiki(self.wiki, name="South Entrance")

    def _get(self, location: Location | None = None) -> dict:
        location = location or self.location
        request = self.factory.get(f"/location/{location.slug}/pins/json/")
        request.user = self.viewer
        response = LocationDetailPinJsonView.as_view()(request, location_slug=location.slug)
        self.assertEqual(response.status_code, 200)
        return json.loads(response.content)

    def _find(self, data: dict, wiki: Wiki) -> dict | None:
        for item in data["detail_pins"]:
            if item.get("uuid") == str(wiki.uuid):
                return item
        return None

    def test_child_wikis_are_returned(self) -> None:
        data = self._get()
        self.assertIsNotNone(self._find(data, self.child_a))
        self.assertIsNotNone(self._find(data, self.child_b))

    def test_root_wiki_itself_is_not_returned(self) -> None:
        data = self._get()
        self.assertIsNone(self._find(data, self.wiki))

    def test_child_wiki_name_is_included(self) -> None:
        data = self._get()
        item = self._find(data, self.child_a)
        self.assertEqual(item["name"], "North Entrance")

    def test_empty_location_returns_empty_list(self) -> None:
        empty_location = _make_location()
        data = self._get(empty_location)
        self.assertEqual(data["detail_pins"], [])
