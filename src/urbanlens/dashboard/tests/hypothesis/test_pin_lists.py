"""Tests for pin list regressions found while polishing the pin list feature.

Invariants verified:
  - serialize_form_criteria preserves the "name" (pin-name-contains) field -
    it was previously dropped silently, so a saved filter or smart list built
    from a name search lost that criterion the moment it was saved.
  - PinListMarkupMapView's "no pins with coordinates" error is valid JSON (the
    client always calls response.json() on it), not a plain-text body that
    throws a SyntaxError in the browser.
"""

from __future__ import annotations

import json

from django.contrib.auth.models import User
from django.urls import reverse
from hypothesis import HealthCheck, given, settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.pin_list.model import PinList
from urbanlens.dashboard.services.filter_criteria import serialize_form_criteria

_db_settings = settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much])


class SerializeFormCriteriaNamePreservedTests(TestCase):
    """serialize_form_criteria must not drop the `name` filter field."""

    @_db_settings
    @given(name=st.text(min_size=1, max_size=100).filter(lambda s: s.strip()))
    def test_name_round_trips(self, name: str) -> None:
        criteria = serialize_form_criteria({"name": name}, label_groups=None, custom_field_criteria=None)
        self.assertEqual(criteria.get("name"), name.strip())

    def test_blank_name_is_not_stored(self) -> None:
        criteria = serialize_form_criteria({"name": "   "}, label_groups=None, custom_field_criteria=None)
        self.assertNotIn("name", criteria)

    def test_name_alone_is_not_an_empty_criteria_dict(self) -> None:
        # A saved filter made of just a name search must be considered "active"
        # criteria by SavedFilterCreateView, which rejects an empty dict.
        criteria = serialize_form_criteria({"name": "rooftop"}, label_groups=None, custom_field_criteria=None)
        self.assertTrue(criteria)


class PinListMarkupMapErrorIsJsonTests(TestCase):
    """The markup-map endpoint must return JSON even on its error paths."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.profile = self.user.profile

    def test_no_geo_pins_returns_json_error(self) -> None:
        pin_list = baker.make(PinList, profile=self.profile, name="Empty list")
        response = self.client.post(reverse("lists.markup_map", kwargs={"list_uuid": pin_list.uuid}))
        self.assertEqual(response.status_code, 400)
        # Must not raise - this is exactly what broke: the client always calls
        # response.json(), and a plain-text body throws a SyntaxError.
        data = json.loads(response.content)
        self.assertFalse(data["ok"])
        self.assertIn("error", data)
