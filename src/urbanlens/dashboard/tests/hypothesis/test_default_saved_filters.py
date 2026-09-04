"""What a brand-new profile starts with, and what it may not lose.

Three things, all reported together:

- A new profile gets two saved filters, so the main map's filter bar is not
  an empty shelf someone has to learn the formula syntax to fill.
- "Want to Go" was the one default status label without `is_protected`, while
  its four siblings had it - so the label the second filter is built on could
  be deleted out from under it.
- Merging a label *deletes* the source, so every guard that protects a label
  from deletion has to hold on the merge paths too. The single-merge view
  checks both `profile is None` (a global label) and `is_protected`; the bulk
  path checked ownership for every kind but `is_protected` for statuses only.
"""

from __future__ import annotations

import json

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.model import KIND_CATEGORY, KIND_STATUS, KIND_TAG, Label
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.saved_filter.model import SavedFilter


class DefaultSavedFilterTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.profile = baker.make(User).profile

    def test_a_new_profile_gets_both_filters(self) -> None:
        names = set(SavedFilter.objects.filter(profile=self.profile).values_list("name", flat=True))

        self.assertEqual(names, {"Visited", "To Visit"})

    def test_each_filter_is_visually_distinguishable(self) -> None:
        """They sit side by side on the map's filter bar."""
        filters = list(SavedFilter.objects.filter(profile=self.profile))

        self.assertTrue(all(saved.icon for saved in filters), "a filter with no icon renders as a blank button")
        self.assertTrue(all(saved.color for saved in filters))
        self.assertEqual(len({saved.color for saved in filters}), 2, "two filters sharing a colour defeat the point")

    def test_the_visited_filter_matches_visited_pins_only(self) -> None:
        visited = Label.objects.get(profile=self.profile, name="Visited", kind=KIND_STATUS)
        seen = baker.make(Pin, profile=self.profile, parent_pin=None)
        seen.labels.add(visited)
        unseen = baker.make(Pin, profile=self.profile, parent_pin=None)

        matched = self._apply("Visited")

        self.assertIn(seen.pk, matched)
        self.assertNotIn(unseen.pk, matched)

    def test_the_to_visit_filter_takes_wanted_or_high_priority_but_not_visited(self) -> None:
        """The reported rule: not Visited, not Demolished, and (Want to Go OR priority >= 4)."""
        wanted = Label.objects.get(profile=self.profile, name="Want to Go", kind=KIND_STATUS)
        visited = Label.objects.get(profile=self.profile, name="Visited", kind=KIND_STATUS)
        demolished = Label.objects.get(profile=self.profile, name="Demolished", kind=KIND_STATUS)

        by_label = baker.make(Pin, profile=self.profile, parent_pin=None, priority=1)
        by_label.labels.add(wanted)
        by_priority = baker.make(Pin, profile=self.profile, parent_pin=None, priority=4)
        already_seen = baker.make(Pin, profile=self.profile, parent_pin=None, priority=5)
        already_seen.labels.add(visited)
        gone = baker.make(Pin, profile=self.profile, parent_pin=None, priority=5)
        gone.labels.add(demolished)
        ordinary = baker.make(Pin, profile=self.profile, parent_pin=None, priority=2)

        matched = self._apply("To Visit")

        self.assertIn(by_label.pk, matched, "a wanted pin with low priority still qualifies")
        self.assertIn(by_priority.pk, matched, "a high-priority pin qualifies without the label")
        self.assertNotIn(already_seen.pk, matched, "a visited pin is not somewhere to go")
        self.assertNotIn(gone.pk, matched, "a demolished pin is not somewhere to go")
        self.assertNotIn(ordinary.pk, matched)

    def _apply(self, name: str) -> set[int]:
        from urbanlens.dashboard.services.search.filter_criteria import deserialize_criteria

        saved = SavedFilter.objects.get(profile=self.profile, name=name)
        criteria = deserialize_criteria(saved.criteria, self.profile)
        return set(
            Pin.objects.filter(profile=self.profile)
            .filter_by_criteria(criteria)
            .distinct()
            .values_list("pk", flat=True)
        )


class ProtectedLabelTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def _merge(self, target: Label, source: Label):
        return self.client.post(
            reverse("label.multi_merge", kwargs={"label_kind": "tags"}),
            data=json.dumps({"target_id": target.pk, "source_ids": [source.pk]}),
            content_type="application/json",
        )

    def test_want_to_go_is_protected_like_its_siblings(self) -> None:
        wanted = Label.objects.get(profile=self.profile, name="Want to Go", kind=KIND_STATUS)

        self.assertTrue(wanted.is_protected, "the default filters are built on this label; it must not be deletable")

    def test_a_protected_label_cannot_be_merged_away_in_bulk(self) -> None:
        """Merging deletes the source, so protection has to hold here too."""
        source = baker.make(Label, profile=self.profile, kind=KIND_TAG, name="protected-tag", is_protected=True)
        target = baker.make(Label, profile=self.profile, kind=KIND_TAG, name="ordinary-tag")

        self._merge(target, source)

        self.assertTrue(
            Label.objects.filter(pk=source.pk).exists(), "a protected label was deleted by being merged away"
        )

    def test_a_protected_label_cannot_be_bulk_deleted(self) -> None:
        source = baker.make(Label, profile=self.profile, kind=KIND_CATEGORY, name="protected-cat", is_protected=True)

        self.client.post(
            reverse("label.bulk_delete", kwargs={"label_kind": "categories"}),
            data=json.dumps({"ids": [source.pk]}),
            content_type="application/json",
        )

        self.assertTrue(Label.objects.filter(pk=source.pk).exists())

    def test_an_ordinary_label_still_merges(self) -> None:
        """The guard must not freeze the feature."""
        source = baker.make(Label, profile=self.profile, kind=KIND_TAG, name="ordinary-source")
        target = baker.make(Label, profile=self.profile, kind=KIND_TAG, name="ordinary-target")

        self._merge(target, source)

        self.assertFalse(Label.objects.filter(pk=source.pk).exists())

    def test_a_global_label_is_not_mergeable_as_a_source(self) -> None:
        """Reported as a suspicion; it already held, and must keep holding."""
        global_tag = baker.make(Label, profile=None, kind=KIND_TAG, name="global-tag")
        target = baker.make(Label, profile=self.profile, kind=KIND_TAG, name="mine")

        self._merge(target, global_tag)

        self.assertTrue(Label.objects.filter(pk=global_tag.pk).exists())
