"""Tests for the Organize > Labels "View on map" button.

Each row on the Labels page (tag/category/status) can jump to the main map
pre-filtered to just that label, via a `label_groups` query param the main
map's existing `_restoreFiltersFromUrl()` (map/index.html) already knows how
to apply - see `dashboard_tags.label_map_url`.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY, KIND_STATUS, KIND_TAG, KIND_USER
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.templatetags.dashboard_tags import label_map_url


class LabelMapUrlFilterTests(TestCase):
    def test_builds_a_single_or_group_for_the_label_id(self) -> None:
        url = label_map_url(42)
        parsed = urlparse(url)
        self.assertEqual(parsed.path, reverse("map.view"))
        groups = json.loads(parse_qs(parsed.query)["label_groups"][0])
        self.assertEqual(groups, [{"op": "or", "ids": [42]}])


class LabelRowsViewOnMapButtonTests(TestCase):
    def setUp(self) -> None:
        baker.make(User)  # bootstrap site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)

    def _rows(self, kind: str):
        return self.client.get(reverse("label.rows", kwargs={"label_kind": kind}))

    def test_tag_row_includes_view_on_map_link(self) -> None:
        label = baker.make(Label, profile=self.profile, kind=KIND_TAG, name="Urbex")
        response = self._rows("tags")
        self.assertContains(response, label_map_url(label.id))

    def test_category_row_includes_view_on_map_link(self) -> None:
        # _queryset_for_kind uses .for_profile() (owned-only, excludes global) for
        # category/status rows - unlike tags, which use .visible_to() (global + owned).
        label = baker.make(Label, profile=self.profile, kind=KIND_CATEGORY, name="Factories")
        response = self._rows("category")
        self.assertContains(response, label_map_url(label.id))

    def test_status_row_includes_view_on_map_link(self) -> None:
        label = baker.make(Label, profile=self.profile, kind=KIND_STATUS, name="Abandoned")
        response = self._rows("statuses")
        self.assertContains(response, label_map_url(label.id))

    def test_people_row_has_no_view_on_map_link(self) -> None:
        """People labels don't feed the map's label_groups filter - no button to render."""
        baker.make(Label, profile=self.profile, kind=KIND_USER, name="Alex")
        response = self._rows("people")
        self.assertNotContains(response, "View on map")
