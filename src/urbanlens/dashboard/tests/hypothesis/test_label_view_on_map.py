"""Tests for the Organize > Labels "View on map" button."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.labels import ensure_label
from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY, KIND_STATUS, KIND_TAG, KIND_USER
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.templatetags.dashboard_tags import label_map_url, tag_total_pins


class LabelMapUrlFilterTests(SimpleTestCase):
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

    def _pin_labelled(self, *labels: Label) -> Pin:
        pin = baker.make(Pin, profile=self.profile)
        pin.labels.add(*labels)
        return pin

    def test_tag_row_includes_view_on_map_link(self) -> None:
        label = baker.make(Label, profile=self.profile, kind=KIND_TAG, name="Urbex")
        self._pin_labelled(label)
        response = self._rows("tags")
        self.assertContains(response, label_map_url(label.id))

    def test_category_row_includes_view_on_map_link(self) -> None:
        # _queryset_for_kind uses .for_profile() (owned-only, excludes global) for
        # category/status rows - unlike tags, which use .visible_to() (global + owned).
        label = baker.make(Label, profile=self.profile, kind=KIND_CATEGORY, name="Factories")
        self._pin_labelled(label)
        response = self._rows("category")
        self.assertContains(response, label_map_url(label.id))

    def test_status_row_includes_view_on_map_link(self) -> None:
        label = ensure_label(profile=self.profile, kind=KIND_STATUS, name="Abandoned")
        self._pin_labelled(label)
        response = self._rows("statuses")
        self.assertContains(response, label_map_url(label.id))

    def test_people_row_has_no_view_on_map_link(self) -> None:
        """People labels don't feed the map's label_groups filter - no button to render."""
        baker.make(Label, profile=self.profile, kind=KIND_USER, name="Alex")
        response = self._rows("people")
        self.assertNotContains(response, "View on map")


class LabelRowsEmptyLabelButtonTests(TestCase):
    """A label with no pins anywhere in its subtree gets an inert button."""

    def setUp(self) -> None:
        baker.make(User)  # bootstrap site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)

    def _rows(self, kind: str = "tags"):
        return self.client.get(reverse("label.rows", kwargs={"label_kind": kind}))

    def _card(self, response, label_id: int) -> str:
        """Return just one label's card markup.

        The tag rows also carry every seeded global tag, most of which have no pins and so legitimately render
        disabled - assertions about a specific label have to be scoped to its own card."""
        content = response.content.decode()
        start = content.index(f'id="tag-card-{label_id}"')
        end = content.find('<div class="tag-card"', start)
        return content[start:] if end == -1 else content[start:end]

    def test_empty_label_button_is_disabled_and_unlinked(self) -> None:
        label = baker.make(Label, profile=self.profile, kind=KIND_TAG, name="Unused")
        response = self._rows()
        card = self._card(response, label.id)
        self.assertNotIn(label_map_url(label.id), card)
        self.assertIn('aria-disabled="true"', card)
        self.assertIn("No pins in this label or its sub-labels", card)

    def test_label_with_pins_only_on_a_descendant_stays_enabled(self) -> None:
        """Map filtering expands a parent to its whole subtree, so the parent isn't empty."""
        parent = baker.make(Label, profile=self.profile, kind=KIND_TAG, name="Industrial")
        child = baker.make(Label, profile=self.profile, kind=KIND_TAG, name="Steel Mill")
        child.parents.add(parent)
        pin = baker.make(Pin, profile=self.profile)
        pin.labels.add(child)

        response = self._rows()
        self.assertContains(response, label_map_url(parent.id))
        self.assertContains(response, label_map_url(child.id))
        self.assertNotIn('aria-disabled="true"', self._card(response, parent.id))

    def test_parent_stays_disabled_when_no_descendant_has_pins(self) -> None:
        parent = baker.make(Label, profile=self.profile, kind=KIND_TAG, name="Industrial")
        child = baker.make(Label, profile=self.profile, kind=KIND_TAG, name="Steel Mill")
        child.parents.add(parent)

        response = self._rows()
        self.assertNotContains(response, label_map_url(parent.id))
        self.assertNotContains(response, label_map_url(child.id))

    def test_the_first_paint_renders_the_same_button_state_as_the_rows_endpoint(self) -> None:
        """The page computes the real totals, so an empty label is greyed out on the
        first paint rather than after a backfill. Both halves are asserted because
        the two render paths draw the same cards and must not disagree.
        """
        empty = baker.make(Label, profile=self.profile, kind=KIND_TAG, name="Unused")
        used = baker.make(Label, profile=self.profile, kind=KIND_TAG, name="Used")
        self._pin_labelled(used)

        response = self.client.get(reverse("organize.index"), {"tab": "tags"})

        empty_card = self._card(response, empty.id)
        self.assertNotIn(label_map_url(empty.id), empty_card)
        self.assertIn('aria-disabled="true"', empty_card)

        used_card = self._card(response, used.id)
        self.assertIn(label_map_url(used.id), used_card)
        self.assertNotIn('aria-disabled="true"', used_card)

    def _pin_labelled(self, *labels: Label) -> Pin:
        pin = baker.make(Pin, profile=self.profile)
        pin.labels.add(*labels)
        return pin


class TagTotalPinsMemoTests(TestCase):
    """`tag_total_pins` is read up to three times per card - it must only compute once."""

    def setUp(self) -> None:
        baker.make(User)  # bootstrap site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)

    def test_repeated_calls_hit_the_memo(self) -> None:
        parent = baker.make(Label, profile=self.profile, kind=KIND_TAG, name="Industrial")
        child = baker.make(Label, profile=self.profile, kind=KIND_TAG, name="Steel Mill")
        child.parents.add(parent)
        pin = baker.make(Pin, profile=self.profile)
        pin.labels.add(child)

        fresh = Label.objects.get(pk=parent.pk)
        self.assertEqual(tag_total_pins(fresh), 1)
        with self.assertNumQueries(0):
            self.assertEqual(tag_total_pins(fresh), 1)

    def test_zero_total_is_memoized_too(self) -> None:
        label = Label.objects.get(pk=baker.make(Label, profile=self.profile, kind=KIND_TAG, name="Unused").pk)
        self.assertEqual(tag_total_pins(label), 0)
        with self.assertNumQueries(0):
            self.assertEqual(tag_total_pins(label), 0)
