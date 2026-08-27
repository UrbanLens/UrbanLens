"""Reordering labels costs a fixed number of queries, not one per label.

Drag-and-drop reorder posted the full id list and issued one ``UPDATE`` per id, so
dragging a single label in a list of 50 wrote 50 statements. The row count is chosen
by the user's own label list, so this grows without any bound the code controls.

Two behaviours have to survive the collapse:

- Ids that are not the requesting profile's - or are the wrong kind - are silently
  ignored rather than erroring. The per-row form got this from re-filtering on
  ``profile``/``kind`` inside the loop, so the filter has to move, not disappear.
- No ``post_save`` receivers run. ``queryset.update()`` never fired them, so
  ``bulk_update`` (which also does not) keeps this identical - worth stating because
  ``Label`` has receivers that would matter if either call did fire them.
"""

from __future__ import annotations

import json

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.meta import KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.profile.model import Profile


class LabelReorderQueryCountTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)
        self.url = reverse("label.reorder", kwargs={"label_kind": KIND_TAG})
        self._next_tag = 0

    def _make_tags(self, count: int) -> list[Label]:
        """Names run off a per-test counter - restarting at 0 collides on ``uq_label_profile_name_kind_ci``."""
        start = self._next_tag
        self._next_tag += count
        return [baker.make(Label, profile=self.profile, kind=KIND_TAG, name=f"Tag {i}") for i in range(start, start + count)]

    def _reorder(self, ids: list[int]):
        return self.client.post(self.url, data=json.dumps({"tag_ids": ids}), content_type="application/json")

    def test_the_query_count_does_not_grow_with_the_number_of_labels(self) -> None:
        """Five labels and twenty-five must cost the same, or it is still per-row."""
        small = [tag.pk for tag in self._make_tags(5)]
        with CaptureQueriesContext(connection) as small_ctx:
            self.assertEqual(self._reorder(list(reversed(small))).status_code, 200)

        large = [tag.pk for tag in self._make_tags(25)]
        with CaptureQueriesContext(connection) as large_ctx:
            self.assertEqual(self._reorder(list(reversed(large))).status_code, 200)

        self.assertEqual(
            len(small_ctx.captured_queries),
            len(large_ctx.captured_queries),
            f"reorder scales with label count: 5 labels took {len(small_ctx.captured_queries)} "
            f"queries, 25 took {len(large_ctx.captured_queries)}",
        )

    def test_reordering_applies_the_posted_sequence(self) -> None:
        tags = self._make_tags(4)
        desired = [tags[2].pk, tags[0].pk, tags[3].pk, tags[1].pk]

        self.assertEqual(self._reorder(desired).status_code, 200)

        ordered = list(Label.objects.filter(pk__in=desired).order_by("-order").values_list("pk", flat=True))
        self.assertEqual(ordered, desired)

    def test_ids_belonging_to_another_profile_are_ignored_not_errors(self) -> None:
        mine = self._make_tags(2)
        stranger = baker.make("auth.User")
        theirs = baker.make(Label, profile=Profile.objects.get(user=stranger), kind=KIND_TAG, name="Theirs")
        before = Label.objects.get(pk=theirs.pk).order

        response = self._reorder([theirs.pk, mine[0].pk, mine[1].pk])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Label.objects.get(pk=theirs.pk).order, before)
