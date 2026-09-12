"""A stranger's places must not cost a viewer anything.

``accessible_domain_ids`` runs on every wiki page, every wiki-scoped API call
and - via ``services.media.access`` - every request for a photo the viewer does
not own. Its aggregate-earning step read the whole ``Place`` table twice per
call: every aggregate on the site, and every ``MEMBER_OF`` row beneath them.
Nothing in that read is scoped to the viewer, so a user who splits their
properties into members makes every other user's media fetch more expensive,
which is exactly the availability coupling the site is not allowed to have.

Two properties, and they pull in opposite directions:

- **Cost is the viewer's own.** Rows read must not grow with data the viewer
  cannot reach. The scaling test below is the one that was red.
- **Reach is unchanged.** Narrowing what an access check reads is the textbook
  way to accidentally widen (or break) an access check, so the earning rule is
  held to the implementation it replaced over every subset of a nested
  aggregate lineage - all of them, not a chosen few.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import connection
from model_bakery import baker

from urbanlens.core.tests.agreement import assert_agrees
from urbanlens.core.tests.endpoint_scaling import _row_counting_wrapper
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.place.model import Place, PlaceKind, PlaceRelation
from urbanlens.dashboard.services.wiki.wiki_access import MAX_EARNING_ROUNDS, _earn_aggregates, accessible_domain_ids

from .test_places_access_predicate import pin_on
from .test_places_campus import make_place, square


def _earn_aggregates_site_wide(domains: set[int]) -> set[int]:
    """The pre-2026-09-12 implementation, kept as the oracle for the new one.

    Reads every aggregate on the site and every member row beneath one, then
    sweeps to a fixpoint. Correct, and the reason this file exists.
    """
    aggregate_roots = dict(Place.objects.filter(is_aggregate=True).values_list("pk", "domain_root_id"))
    if not aggregate_roots:
        return set(domains)

    members: dict[int, set[int]] = {}
    for parent_id, child_root in Place.objects.filter(
        parent_id__in=list(aggregate_roots), parent_relation=PlaceRelation.MEMBER_OF
    ).values_list("parent_id", "domain_root_id"):
        members.setdefault(parent_id, set()).add(child_root)

    earned = set(domains)
    for _ in range(MAX_EARNING_ROUNDS):
        added = False
        for aggregate_id, member_roots in members.items():
            root = aggregate_roots[aggregate_id]
            if root in earned or not member_roots:
                continue
            if member_roots <= earned:
                earned.add(root)
                added = True
        if not added:
            return earned
    return earned


def _stranger_aggregate(index: int) -> None:
    """A two-member site, far from anything the viewer pins, owned by nobody."""
    site = make_place(PlaceKind.SITE, None, name=f"stranger-{index}")
    for member in range(2):
        make_place(
            PlaceKind.PARCEL,
            square(-100.0 + index * 0.1 + member * 0.02, 35.0, 0.005),
            parent=site,
            relation=PlaceRelation.MEMBER_OF,
        )


class StrangerPlacesCostTheViewerNothingTests(TestCase):
    """The viewer's access check must read only the viewer's own reach."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile
        self.parcel = make_place(PlaceKind.PARCEL, square(-74.0, 40.0, 0.01))
        pin_on(self.profile, self.parcel, lat=40.0, lng=-74.0)

    def _rows_read(self) -> int:
        """Rows every statement of one access check returned."""
        totals = [0]
        with connection.execute_wrapper(_row_counting_wrapper(totals)):
            accessible_domain_ids(self.profile)
        return totals[0]

    def test_rows_read_do_not_grow_with_places_the_viewer_cannot_reach(self) -> None:
        for index in range(2):
            _stranger_aggregate(index)
        baseline = self._rows_read()

        for index in range(2, 32):
            _stranger_aggregate(index)
        after = self._rows_read()

        self.assertEqual(
            after,
            baseline,
            f"Thirty aggregates belonging to nobody the viewer knows changed the viewer's access check from {baseline} rows to {after}.",
        )

    def test_the_viewer_still_reaches_their_own_domain(self) -> None:
        """The cheap answer is still the right answer."""
        for index in range(4):
            _stranger_aggregate(index)
        self.assertEqual(accessible_domain_ids(self.profile), {self.parcel.domain_root_id})


class EarningAgreesWithTheSiteWideSweepTests(TestCase):
    """Narrowing the read must not change one earning decision."""

    def setUp(self) -> None:
        super().setUp()
        # campus -> {A, B}; A -> {A1, A2}; B -> {B1, B2}. Two tiers of earning,
        # and four leaves whose subsets cover every partial-coverage case.
        self.campus = make_place(PlaceKind.SITE, None, name="campus")
        self.parcel_a = make_place(
            PlaceKind.PARCEL, None, name="A", parent=self.campus, relation=PlaceRelation.MEMBER_OF
        )
        self.parcel_b = make_place(
            PlaceKind.PARCEL, None, name="B", parent=self.campus, relation=PlaceRelation.MEMBER_OF
        )
        self.leaves = [
            make_place(
                PlaceKind.PARCEL,
                square(-74.0 + i * 0.05, 40.0, 0.01),
                name=f"leaf-{i}",
                parent=parent,
                relation=PlaceRelation.MEMBER_OF,
            )
            for i, parent in enumerate((self.parcel_a, self.parcel_a, self.parcel_b, self.parcel_b))
        ]
        for place in (self.campus, self.parcel_a, self.parcel_b, *self.leaves):
            place.refresh_from_db()
        # An unrelated aggregate nobody in this test earns, so the oracle has
        # something to read that the candidate must be able to ignore.
        _stranger_aggregate(99)

    def _subsets(self) -> list[set[int]]:
        """Every combination of the four leaf domains."""
        roots = [leaf.domain_root_id for leaf in self.leaves]
        return [{root for index, root in enumerate(roots) if mask >> index & 1} for mask in range(1 << len(roots))]

    def test_every_subset_of_the_lineage_earns_what_it_used_to(self) -> None:
        assert_agrees(
            _earn_aggregates_site_wide,
            _earn_aggregates,
            self._subsets(),
            describe=lambda domains: f"domains={sorted(domains)}",
            label="_earn_aggregates",
        )

    def test_the_battery_actually_exercises_earning(self) -> None:
        """Guard against a battery where every subset earns nothing."""
        outcomes = {len(_earn_aggregates_site_wide(subset)) - len(subset) for subset in self._subsets()}
        self.assertIn(0, outcomes, "No subset earned nothing - the oracle is not being asked the negative case.")
        self.assertTrue(
            any(gained > 0 for gained in outcomes), "No subset earned an aggregate - the agreement test is vacuous."
        )
