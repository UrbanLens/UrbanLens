"""Tests for the explicit membership operations in ``services.pins.pin_list_membership``.

These are the add/remove/reorder functions extracted from
``controllers.pin_lists``, plus the shared-computation resync extracted from
``controllers.saved_filters``. The point of the extraction is that the web UI
and the external API cannot drift apart, so the rules are asserted here once
rather than per-caller.

``reorder_list_items`` gets a Hypothesis property test: it is pure ordering
logic over a permutation, which is exactly the shape ``@given`` is good at.
The view-level tests stay plain, per this repo's rule that ``@given`` and
``self.client`` do not mix.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from model_bakery import baker

from hypothesis import given, settings, strategies as st
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_list.model import PinList, PinListItem
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.saved_filter.model import SavedFilter
from urbanlens.dashboard.models.site_settings.model import SiteSettings
from urbanlens.dashboard.services.pins.pin_creation import create_pin_for_profile
from urbanlens.dashboard.services.pins.pin_list_membership import (
    add_pins_to_list,
    filter_matching_ids,
    remove_pins_from_list,
    reorder_list_items,
    resync_lists_for_saved_filter,
)


class MembershipServiceTestCase(TestCase):
    """Shared fixture: one profile with a list and a handful of pins."""

    def setUp(self) -> None:
        baker.make(User)
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.pin_list = PinList.objects.create(profile=self.profile, name="Favorites")

    def _pins(self, count: int) -> list:
        return [
            create_pin_for_profile(
                self.profile, name=f"Pin {i}", latitude=40.0 + i / 100, longitude=-70.0 - i / 100
            ).pin
            for i in range(count)
        ]


class AddPinsToListTests(MembershipServiceTestCase):
    """``add_pins_to_list``."""

    def test_adds_and_orders_from_the_current_count(self) -> None:
        first, second = self._pins(2)
        add_pins_to_list(self.pin_list, [first])
        result = add_pins_to_list(self.pin_list, [second])

        self.assertEqual(result.added, 1)
        orders = list(self.pin_list.items.order_by("order").values_list("order", flat=True))
        self.assertEqual(orders, [0, 1])

    def test_duplicates_within_one_call_are_collapsed(self) -> None:
        """Two references to one pin must not violate uq_pin_list_item."""
        pin = self._pins(1)[0]
        result = add_pins_to_list(self.pin_list, [pin, pin])
        self.assertEqual(result.added, 1)
        self.assertEqual(self.pin_list.items.count(), 1)

    def test_pins_already_on_the_list_are_skipped(self) -> None:
        pin = self._pins(1)[0]
        add_pins_to_list(self.pin_list, [pin])
        result = add_pins_to_list(self.pin_list, [pin])
        self.assertEqual(result.added, 0)
        self.assertEqual(self.pin_list.items.count(), 1)

    def test_cap_truncates_and_reports(self) -> None:
        site_settings = SiteSettings.get_current()
        site_settings.max_pins_per_list = 2
        site_settings.save()

        result = add_pins_to_list(self.pin_list, self._pins(5))
        self.assertEqual(result.added, 2)
        self.assertEqual(result.skipped_over_cap, 3)
        self.assertEqual(result.max_pins, 2)

    def test_zero_cap_means_unlimited(self) -> None:
        site_settings = SiteSettings.get_current()
        site_settings.max_pins_per_list = 0
        site_settings.save()

        result = add_pins_to_list(self.pin_list, self._pins(4))
        self.assertEqual(result.added, 4)
        self.assertEqual(result.skipped_over_cap, 0)

    def test_default_provenance_is_manual(self) -> None:
        """Manual items are what a later resync must never remove."""
        add_pins_to_list(self.pin_list, self._pins(1))
        self.assertEqual(self.pin_list.items.first().added_via, PinListItem.ADDED_MANUAL)


class RemovePinsFromListTests(MembershipServiceTestCase):
    """``remove_pins_from_list``."""

    def test_removes_and_returns_the_count(self) -> None:
        pins = self._pins(3)
        add_pins_to_list(self.pin_list, pins)
        removed = remove_pins_from_list(self.pin_list, [pins[0].pk, pins[1].pk])
        self.assertEqual(removed, 2)
        self.assertEqual(self.pin_list.items.count(), 1)

    def test_unknown_ids_are_ignored(self) -> None:
        pins = self._pins(1)
        add_pins_to_list(self.pin_list, pins)
        self.assertEqual(remove_pins_from_list(self.pin_list, [999_999]), 0)

    def test_gaps_in_order_are_tolerated_not_renumbered(self) -> None:
        pins = self._pins(3)
        add_pins_to_list(self.pin_list, pins)
        remove_pins_from_list(self.pin_list, [pins[1].pk])
        self.assertEqual(list(self.pin_list.items.order_by("order").values_list("order", flat=True)), [0, 2])

    def test_does_not_touch_another_lists_items(self) -> None:
        other_list = PinList.objects.create(profile=self.profile, name="Other")
        pin = self._pins(1)[0]
        add_pins_to_list(self.pin_list, [pin])
        add_pins_to_list(other_list, [pin])

        remove_pins_from_list(self.pin_list, [pin.pk])
        self.assertEqual(other_list.items.count(), 1)


class ReorderListItemsTests(MembershipServiceTestCase):
    """``reorder_list_items``."""

    def test_orders_follow_the_submitted_sequence(self) -> None:
        pins = self._pins(3)
        add_pins_to_list(self.pin_list, pins)
        items = list(self.pin_list.items.order_by("order"))
        new_order = [items[2].pk, items[0].pk, items[1].pk]

        self.assertEqual(reorder_list_items(self.pin_list, new_order), 3)
        self.assertEqual([item.pk for item in self.pin_list.items.order_by("order")], new_order)

    def test_foreign_ids_are_ignored_leniently(self) -> None:
        pins = self._pins(2)
        add_pins_to_list(self.pin_list, pins)
        items = list(self.pin_list.items.order_by("order"))

        reordered = reorder_list_items(self.pin_list, [999_999, items[1].pk, items[0].pk])
        self.assertEqual(reordered, 2)
        # The foreign id still consumed index 0, so the result is sparse but
        # correctly ordered - matching the web UI's existing behavior.
        self.assertEqual([item.pk for item in self.pin_list.items.order_by("order")], [items[1].pk, items[0].pk])

    def test_items_of_another_list_are_not_reordered(self) -> None:
        other_list = PinList.objects.create(profile=self.profile, name="Other")
        pin = self._pins(1)[0]
        add_pins_to_list(other_list, [pin])
        foreign_item = other_list.items.first()

        self.assertEqual(reorder_list_items(self.pin_list, [foreign_item.pk]), 0)
        foreign_item.refresh_from_db()
        self.assertEqual(foreign_item.order, 0)


class ReorderPropertyTests(TestCase):
    """Property: any permutation of a list's items yields contiguous 0..n-1 order.

    Pure ordering logic, so ``@given`` applies - note there is no ``self.client``
    anywhere here, which this repo's TestCase does not tolerate under ``@given``.

    Fixtures are built with ``baker.make`` rather than
    ``create_pin_for_profile``: the reorder path only reads ``PinListItem``
    rows, and running the full pin-creation pipeline (geocoding, enrichment,
    smart-list signals) once per generated example makes the property test
    orders of magnitude slower for no additional coverage.
    """

    @given(st.permutations(range(5)))
    @settings(deadline=None, max_examples=10)
    def test_permutation_in_contiguous_out(self, permutation) -> None:
        profile = baker.make(User).profile
        pin_list = baker.make(PinList, profile=profile)
        items = [
            baker.make(PinListItem, pin_list=pin_list, pin=baker.make("dashboard.Pin", profile=profile), order=i)
            for i in range(5)
        ]
        submitted = [items[i].pk for i in permutation]

        self.assertEqual(reorder_list_items(pin_list, submitted), 5)
        result = list(pin_list.items.order_by("order"))
        self.assertEqual([item.pk for item in result], submitted)
        self.assertEqual([item.order for item in result], list(range(5)))


class ResyncListsForSavedFilterTests(MembershipServiceTestCase):
    """``resync_lists_for_saved_filter``."""

    def test_copies_criteria_into_every_derived_list(self) -> None:
        saved = SavedFilter.objects.create(profile=self.profile, name="F", criteria={"min_rating": 5})
        for name in ("A", "B"):
            PinList.objects.create(
                profile=self.profile,
                name=name,
                is_smart=True,
                smart_filter={"min_rating": 1},
                source_saved_filter=saved,
            )

        self.assertEqual(resync_lists_for_saved_filter(saved), 2)
        for derived in PinList.objects.filter(source_saved_filter=saved):
            self.assertEqual(derived.smart_filter, {"min_rating": 5})

    def test_returns_zero_when_nothing_is_derived(self) -> None:
        saved = SavedFilter.objects.create(profile=self.profile, name="F", criteria={"min_rating": 5})
        self.assertEqual(resync_lists_for_saved_filter(saved), 0)

    def test_unrelated_lists_are_untouched(self) -> None:
        saved = SavedFilter.objects.create(profile=self.profile, name="F", criteria={"min_rating": 5})
        unrelated = PinList.objects.create(profile=self.profile, name="Unrelated", smart_filter={"min_rating": 2})

        resync_lists_for_saved_filter(saved)
        unrelated.refresh_from_db()
        self.assertEqual(unrelated.smart_filter, {"min_rating": 2})


class FilterMatchingIdsExcludesChildPinsTests(MembershipServiceTestCase):
    """``filter_matching_ids`` must exclude detail/child pins, matching every saved-filter
    preview call site (``controllers/saved_filters.py``, which chains ``.root_pins()`` before
    ``filter_by_criteria``). Regression for the still-live ``docs/PROBLEMS.md:585`` bug this
    audit confirmed: without it, a child pin could enter smart-list membership even though its
    own filter preview would never have shown it. See docs/audits/GOALS_CODE_AUDIT.md
    ("Lists: filter/manual reconciliation")."""

    def setUp(self) -> None:
        super().setUp()
        self.pin_list.is_smart = True
        self.pin_list.smart_filter = {"name": "Bunker"}
        self.pin_list.save(update_fields=["is_smart", "smart_filter"])
        self.root_pin = create_pin_for_profile(self.profile, name="Bunker Alpha", latitude=40.0, longitude=-70.0).pin
        self.child_pin = Pin.objects.create(
            profile=self.profile,
            parent_pin=self.root_pin,
            location=self.root_pin.location,
            name="Bunker Alpha Sub-room",
        )

    def test_a_root_pin_matching_the_filter_is_included(self) -> None:
        self.assertIn(self.root_pin.pk, filter_matching_ids(self.pin_list))

    def test_a_child_pin_matching_the_filter_is_excluded(self) -> None:
        self.assertNotIn(self.child_pin.pk, filter_matching_ids(self.pin_list))
