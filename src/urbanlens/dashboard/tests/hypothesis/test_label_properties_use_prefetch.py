"""``pin.categories`` and friends must read the prefetch cache, not re-query.

The accessors used to be ``self.labels.all().categories()``. Chaining a queryset
method onto ``.all()`` builds a *new* queryset, and a new queryset ignores the
prefetch cache - so a caller who had carefully done
``prefetch_related("labels")`` still paid one query per kind per row. Rendering a
list of pins cost three extra queries each, silently, while looking prefetched.

Measured before the fix, for 25 pins: 2 queries to load them, 77 once the three
properties were touched. ``services.map_pins.payload`` already avoided this by
filtering the prefetched list in Python; the mixin now does the same thing for
everyone.

The assertion compares "load the pins" against "load the pins and touch every
accessor" rather than pinning a total, so it keeps its meaning if the number of
queries needed to load a pin changes.
"""

from __future__ import annotations

from django.db import connection
from django.db.models import Prefetch
from django.test.utils import CaptureQueriesContext
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY, KIND_STATUS, KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile

_PIN_COUNT = 10


class LabelPropertyPrefetchTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = Profile.objects.get(user=baker.make("auth.User"))
        for index in range(_PIN_COUNT):
            pin = baker.make(Pin, profile=self.profile)
            for kind in (KIND_CATEGORY, KIND_TAG, KIND_STATUS):
                pin.labels.add(baker.make(Label, profile=self.profile, kind=kind, name=f"{kind}-{index}"))

    def _pins(self) -> list[Pin]:
        return list(
            Pin.objects.filter(profile=self.profile).prefetch_related(
                Prefetch("labels", queryset=Label.objects.exclude(kind="user")),
            ),
        )

    def _count(self, touch) -> int:
        with CaptureQueriesContext(connection) as captured:
            for pin in self._pins():
                touch(pin)
        return len(captured.captured_queries)

    def test_touching_every_accessor_costs_no_extra_queries(self) -> None:
        """The property under test: prefetched labels are reused, not re-fetched."""
        baseline = self._count(lambda pin: None)

        # list() forces evaluation: the old form returned a *lazy* queryset, so
        # merely naming the attribute issued no query and measured nothing.
        touched = self._count(lambda pin: [list(pin.categories), list(pin.tags), list(pin.statuses)])

        self.assertEqual(
            touched,
            baseline,
            f"the accessors re-queried: {baseline} -> {touched} queries for {_PIN_COUNT} pins",
        )

    def test_the_accessors_still_return_the_right_labels(self) -> None:
        """Cheap is worthless if it is also wrong."""
        pin = self._pins()[0]

        self.assertEqual([label.kind for label in pin.categories], [KIND_CATEGORY])
        self.assertEqual([label.kind for label in pin.tags], [KIND_TAG])
        self.assertEqual([label.kind for label in pin.statuses], [KIND_STATUS])

    def test_they_work_without_a_prefetch(self) -> None:
        """Not every caller prefetches; those must still get correct answers."""
        pin = Pin.objects.filter(profile=self.profile).first()

        self.assertEqual([label.kind for label in pin.categories], [KIND_CATEGORY])

    def test_a_pin_with_no_labels_returns_empty(self) -> None:
        bare = baker.make(Pin, profile=self.profile)

        self.assertEqual(bare.categories, [])
        self.assertEqual(bare.tags, [])
        self.assertEqual(bare.statuses, [])

    def test_wiki_shares_the_same_accessors(self) -> None:
        """Both models carry `labels`; the mixin is why they cannot drift apart."""
        from urbanlens.dashboard.models.wiki.model import Wiki

        wiki = baker.make(Wiki)
        wiki.labels.add(baker.make(Label, profile=self.profile, kind=KIND_TAG, name="wiki-tag"))

        self.assertEqual([label.name for label in wiki.tags], ["wiki-tag"])
        self.assertEqual(wiki.categories, [])
