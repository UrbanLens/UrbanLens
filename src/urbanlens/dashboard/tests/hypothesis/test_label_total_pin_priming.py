"""Priming subtree pin counts must be faster *and* produce identical numbers.

``Label.total_pin_count`` walks the label hierarchy with a BFS that issues one
query per node visited, adds a ``Count`` aggregate, and memoizes only on the
instance it was called on. Rendering a page of labels therefore cost
O(labels x subtree) queries: measured on the Organize page's deferred rows
endpoint, 143 labels cost 113 (tags) and 146 (categories) queries, growing by
exactly one per added label.

``prime_total_pin_counts`` resolves the same numbers in a fixed three queries by
loading the edge list once and traversing in Python. That is only worth having if
it agrees with the original in every case, so these tests assert equality against
the unprimed path rather than against hand-written expected totals - a
hand-written number would encode whatever the fast path happens to do.

The awkward cases are the point: multi-level chains (the BFS is not
direct-children-only), diamonds where one label is reachable by two routes and
must not be counted twice, and cycles, which the original BFS tolerates and any
replacement must too.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile


class LabelTotalPinPrimingTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self._locations = 0

    def _label(self, name: str) -> Label:
        return baker.make(Label, profile=self.profile, kind="tag", name=name)

    def _pin_on(self, label: Label) -> Pin:
        self._locations += 1
        pin = baker.make(
            Pin,
            profile=self.profile,
            location=baker.make(
                Location, latitude=40.0 + self._locations / 100, longitude=-70.0 - self._locations / 100
            ),
        )
        pin.labels.add(label)
        return pin

    def _unprimed(self, label: Label) -> int:
        """The original per-instance computation, on a freshly loaded object."""
        return Label.objects.get(pk=label.pk).total_pin_count()

    def _primed(self, labels: list[Label]) -> dict[int, int]:
        fresh = list(Label.objects.filter(pk__in=[label.pk for label in labels]))
        Label.prime_total_pin_counts(fresh)
        return {label.pk: label.total_pin_count() for label in fresh}

    def test_a_flat_label_matches(self) -> None:
        label = self._label("flat")
        self._pin_on(label)
        self._pin_on(label)

        self.assertEqual(self._primed([label])[label.pk], self._unprimed(label))

    def test_a_multi_level_chain_matches(self) -> None:
        """Grandchildren count too - the walk is not direct-children-only."""
        root, mid, leaf = self._label("root"), self._label("mid"), self._label("leaf")
        mid.parents.add(root)
        leaf.parents.add(mid)
        self._pin_on(root)
        self._pin_on(mid)
        self._pin_on(leaf)

        self.assertEqual(self._primed([root])[root.pk], self._unprimed(root))
        self.assertEqual(self._unprimed(root), 3)

    def test_a_diamond_does_not_double_count(self) -> None:
        """One label reachable by two routes is still one label."""
        root, left, right, shared = self._label("root"), self._label("l"), self._label("r"), self._label("shared")
        left.parents.add(root)
        right.parents.add(root)
        shared.parents.add(left)
        shared.parents.add(right)
        self._pin_on(shared)

        self.assertEqual(self._primed([root])[root.pk], self._unprimed(root))
        self.assertEqual(self._unprimed(root), 1)

    def test_a_cycle_terminates_and_matches(self) -> None:
        """The original BFS is explicitly cycle-safe; the fast path must be too."""
        a, b = self._label("a"), self._label("b")
        b.parents.add(a)
        a.parents.add(b)
        self._pin_on(a)
        self._pin_on(b)

        self.assertEqual(self._primed([a])[a.pk], self._unprimed(a))

    def test_a_label_with_no_pins_anywhere_is_zero(self) -> None:
        root, child = self._label("root"), self._label("child")
        child.parents.add(root)

        self.assertEqual(self._primed([root])[root.pk], 0)

    def test_priming_is_a_fixed_number_of_queries(self) -> None:
        """The property that motivated it: cost must not grow with label count."""
        labels = [self._label(f"n{i}") for i in range(12)]
        for i, label in enumerate(labels[1:], start=1):
            label.parents.add(labels[i - 1])
        self._pin_on(labels[-1])

        few = list(Label.objects.filter(pk__in=[label.pk for label in labels[:3]]))
        with CaptureQueriesContext(connection) as small:
            Label.prime_total_pin_counts(few)

        many = list(Label.objects.filter(pk__in=[label.pk for label in labels]))
        with CaptureQueriesContext(connection) as large:
            Label.prime_total_pin_counts(many)

        self.assertEqual(len(large.captured_queries), len(small.captured_queries))
        self.assertLessEqual(len(large.captured_queries), 3)

    def test_edge_query_is_scoped_by_profile_not_unfiltered(self) -> None:
        """Regression: the edge-list query had no WHERE clause at all, fetching
        every profile's label hierarchy site-wide just to prime one label."""
        label = self._label("solo")
        self._pin_on(label)

        with CaptureQueriesContext(connection) as ctx:
            Label.prime_total_pin_counts([label])

        edge_queries = [q["sql"] for q in ctx.captured_queries if "dashboard_labels_parents" in q["sql"]]
        self.assertEqual(len(edge_queries), 1)
        self.assertIn(
            "profile_id",
            edge_queries[0],
            "edge-list query has no profile scoping - it fetches the whole site's hierarchy",
        )

    def test_priming_an_empty_list_touches_nothing(self) -> None:
        with CaptureQueriesContext(connection) as ctx:
            Label.prime_total_pin_counts([])

        self.assertEqual(len(ctx.captured_queries), 0)

    def test_an_unprimed_label_still_computes_itself(self) -> None:
        """Callers rendering a single label must not have to prime first."""
        root, child = self._label("root"), self._label("child")
        child.parents.add(root)
        self._pin_on(child)

        self.assertEqual(Label.objects.get(pk=root.pk).total_pin_count(), 1)
