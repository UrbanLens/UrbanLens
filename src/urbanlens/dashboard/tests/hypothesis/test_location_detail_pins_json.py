"""Tests for LocationDetailPinJsonView - community detail pins with is_mine flag."""

from __future__ import annotations

import json

from django.contrib.auth.models import User
from django.test import RequestFactory
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.detail_pins import LocationDetailPinJsonView
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin


def _make_location() -> Location:
    loc = baker.make(Location)
    if not loc.slug:
        from urbanlens.dashboard.models.location.model import Location as _Loc
        _Loc.objects.filter(pk=loc.pk).update(slug=f"loc-{loc.pk}")
        loc.refresh_from_db()
    return loc


def _make_community_pin(location: Location, user: User, **kwargs) -> Pin:
    profile = user.profile
    wiki, _ = _wiki_for(location)
    return baker.make(
        Pin,
        profile=profile,
        location=location,
        parent_wiki=wiki,
        parent_pin=None,
        latitude=kwargs.get("latitude", 40.0),
        longitude=kwargs.get("longitude", -74.0),
    )


def _wiki_for(location: Location):
    from urbanlens.dashboard.models.wiki.model import Wiki

    return Wiki.objects.get_or_create_for_location(location)


class LocationDetailPinJsonIsMineTests(TestCase):
    """is_mine is True only for pins owned by the requesting user."""

    def setUp(self) -> None:
        self.factory = RequestFactory()
        self.location = _make_location()
        self.viewer = baker.make(User)
        self.other = baker.make(User)
        self.my_pin = _make_community_pin(self.location, self.viewer)
        self.their_pin = _make_community_pin(self.location, self.other)

    def _get(self, user: User) -> dict:
        request = self.factory.get(f"/location/{self.location.slug}/pins/json/")
        request.user = user
        response = LocationDetailPinJsonView.as_view()(request, location_slug=self.location.slug)
        self.assertEqual(response.status_code, 200)
        return json.loads(response.content)

    def _find_pin(self, data: dict, pin: Pin) -> dict | None:
        for item in data["detail_pins"]:
            if item.get("uuid") == str(pin.uuid):
                return item
        return None

    def test_own_pin_has_is_mine_true(self) -> None:
        data = self._get(self.viewer)
        item = self._find_pin(data, self.my_pin)
        self.assertIsNotNone(item)
        self.assertTrue(item["is_mine"])

    def test_other_pin_has_is_mine_false(self) -> None:
        data = self._get(self.viewer)
        item = self._find_pin(data, self.their_pin)
        self.assertIsNotNone(item)
        self.assertFalse(item["is_mine"])

    def test_added_by_set_on_pin_with_profile(self) -> None:
        data = self._get(self.viewer)
        item = self._find_pin(data, self.their_pin)
        self.assertIsNotNone(item)
        self.assertEqual(item["added_by"], self.other.username)

    def test_my_added_by_is_my_username(self) -> None:
        data = self._get(self.viewer)
        item = self._find_pin(data, self.my_pin)
        self.assertIsNotNone(item)
        self.assertEqual(item["added_by"], self.viewer.username)

    def test_all_pins_have_is_mine_key(self) -> None:
        data = self._get(self.viewer)
        for item in data["detail_pins"]:
            self.assertIn("is_mine", item)

    def test_all_pins_have_added_by_key(self) -> None:
        data = self._get(self.viewer)
        for item in data["detail_pins"]:
            self.assertIn("added_by", item)

    def test_empty_location_returns_empty_list(self) -> None:
        empty_location = _make_location()
        request = self.factory.get(f"/location/{empty_location.slug}/pins/json/")
        request.user = self.viewer
        response = LocationDetailPinJsonView.as_view()(request, location_slug=empty_location.slug)
        data = json.loads(response.content)
        self.assertEqual(data["detail_pins"], [])

    def test_viewer_as_other_user_sees_their_own_pin_as_mine(self) -> None:
        data = self._get(self.other)
        item = self._find_pin(data, self.their_pin)
        self.assertIsNotNone(item)
        self.assertTrue(item["is_mine"])

    def test_viewer_as_other_user_sees_original_viewer_pin_as_not_mine(self) -> None:
        data = self._get(self.other)
        item = self._find_pin(data, self.my_pin)
        self.assertIsNotNone(item)
        self.assertFalse(item["is_mine"])
