"""A search's label filter must not let the client choose how much work it is.

``label_groups`` arrives as JSON on the search form. Every id in it was expanded
through a descendant walk that issued one query *per label visited*, and then
added a join to the queryset - with no ceiling on how many ids, or how many
groups, a request may carry (N21 H11). Two separate amplifiers: a deep label
tree turns one id into a query storm, and a long id list turns one request into
a join count no planner handles well.

Both are bounded here. The cap is far above what the filter UI can produce and
far below what hurts, and an over-long filter is trimmed rather than refused -
a saved filter from before the ceiling existed must still return results.
"""

from __future__ import annotations

import json

from django.conf import settings
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile

GROUPS_SETTING = "SEARCH_MAX_LABEL_GROUPS"
IDS_SETTING = "SEARCH_MAX_LABEL_FILTER_IDS"
EXPANSION_SETTING = "SEARCH_MAX_LABEL_EXPANSION"


class _LabelCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make("auth.User")  # the first user is auto-promoted to site admin
        self.profile = Profile.objects.get(user=baker.make("auth.User"))

    def _chain(self, depth: int) -> Label:
        """A label with a descendant chain *depth* long - the deep-tree amplifier."""
        root = baker.make(Label, profile=self.profile)
        current = root
        for _ in range(depth):
            child = baker.make(Label, profile=self.profile)
            child.parents.add(current)
            current = child
        return root

    def _labels(self, count: int) -> list[Label]:
        return [baker.make(Label, profile=self.profile) for _ in range(count)]


class TheSettingsExistTests(_LabelCase):
    """An override_settings of a name nothing reads configures nothing."""

    def test_each_ceiling_is_a_real_setting(self) -> None:
        for name in (GROUPS_SETTING, IDS_SETTING, EXPANSION_SETTING):
            with self.subTest(name):
                self.assertTrue(hasattr(settings, name), f"nothing reads {name}")


class ADeepLabelTreeIsNotAQueryStormTests(_LabelCase):
    def test_expanding_one_id_does_not_cost_a_query_per_label(self) -> None:
        root = self._chain(20)

        with CaptureQueriesContext(connection) as queries:
            Label.get_label_and_descendants(root.pk)

        self.assertLess(
            len(queries.captured_queries),
            10,
            f"walking a 20-deep label chain took {len(queries.captured_queries)} queries",
        )

    def test_the_descendants_are_still_found(self) -> None:
        """The half that stops the test above passing against a walk that stopped walking."""
        root = self._chain(5)

        found = Label.get_label_and_descendants(root.pk)

        self.assertEqual(len(found), 6, "the root plus its five descendants")
        self.assertIn(root.pk, found)

    @override_settings(**{EXPANSION_SETTING: 4})
    def test_the_expansion_stops_at_its_ceiling(self) -> None:
        root = self._chain(50)

        found = Label.get_label_and_descendants(root.pk)

        self.assertLessEqual(len(found), 4)


class TheFilterLengthIsCappedTests(_LabelCase):
    def _parse(self, raw: str) -> list[dict] | None:
        from urbanlens.dashboard.forms.search import SearchForm

        form = SearchForm(data={"label_groups": raw})
        self.assertTrue(form.is_valid(), form.errors)
        return form.parse_label_groups()

    @override_settings(**{IDS_SETTING: 5})
    def test_more_ids_than_the_ceiling_are_trimmed(self) -> None:
        labels = self._labels(30)
        raw = json.dumps([{"op": "and", "ids": [label.pk for label in labels]}])

        groups = self._parse(raw)

        self.assertEqual(sum(len(group["ids"]) for group in groups), 5)

    @override_settings(**{GROUPS_SETTING: 2})
    def test_more_groups_than_the_ceiling_are_trimmed(self) -> None:
        labels = self._labels(6)
        raw = json.dumps([{"op": "and", "ids": [label.pk]} for label in labels])

        groups = self._parse(raw)

        self.assertEqual(len(groups), 2)

    @override_settings(**{GROUPS_SETTING: 8, IDS_SETTING: 20})
    def test_an_ordinary_filter_is_left_alone(self) -> None:
        """The half that stops the tests above passing against a parser that returns nothing."""
        first, second = self._labels(2)
        raw = json.dumps([{"op": "and", "ids": [first.pk]}, {"op": "not", "ids": [second.pk]}])

        groups = self._parse(raw)

        self.assertEqual(groups, [{"op": "and", "ids": [first.pk]}, {"op": "not", "ids": [second.pk]}])

    @override_settings(**{GROUPS_SETTING: 8, IDS_SETTING: 20})
    def test_a_trimmed_filter_still_filters(self) -> None:
        """Trimming must narrow the query, not silently widen it to everything."""
        wanted, other = self._labels(2)
        keep = baker.make(Pin, profile=self.profile)
        keep.labels.add(wanted)
        drop = baker.make(Pin, profile=self.profile)
        drop.labels.add(other)

        groups = self._parse(json.dumps([{"op": "and", "ids": [wanted.pk]}]))
        matched = Pin.objects.filter(profile=self.profile).apply_label_groups(groups).distinct()

        self.assertEqual(list(matched), [keep])
