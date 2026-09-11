"""The list-detail overview map plots a bounded number of pins (P69).

`_items_map_data` had no cap, unlike the near-identical saved-filter preview
map in the same feature, which has had `_PREVIEW_MAP_PIN_LIMIT = 500` all
along - and each marker here carries far more than that one does: name,
address, description, rating, last-visited and every tag chip.

The cap is on the fetch as well as the payload. `_paginated_items_context`
used to materialize every item on the list to serve both the map and one page
of rows, so a payload-only cap would have left the expensive half in place.

Which 500 matters as much as how many. The cap was a slice of the list's own
order - and that order is whatever the owner dragged to the top - so a 5,000-pin
list whose first 500 entries are one city drew a map of that city and offered it
as a map of the list. It is now chosen for coverage (`services/geo/sampling.py`),
and the marker payload is still built for only the chosen rows.
"""

from __future__ import annotations

import itertools

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.pin_lists import _MAP_PIN_LIMIT, _list_items_queryset, _paginated_items_context
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_list.model import PinList, PinListItem
from urbanlens.dashboard.services.pins.pin_list_membership import add_pins_to_list

# Location carries a unique (latitude, longitude) constraint.
_COORDS = itertools.count()


class PinListOverviewMapCapTests(TestCase):
    """A list longer than the cap still renders, with a bounded map payload."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.pin_list = baker.make(PinList, profile=self.profile, name="Big list")

    def _add_pins(self, count: int) -> None:
        for _ in range(count):
            offset = next(_COORDS)
            location = baker.make(
                Location, latitude=f"{40 + offset * 0.001:.6f}", longitude=f"{-74 + offset * 0.001:.6f}"
            )
            pin = baker.make(Pin, profile=self.profile, location=location)
            baker.make(PinListItem, pin_list=self.pin_list, pin=pin)

    def _context(self):
        request = self.client.request(PATH_INFO="/", REQUEST_METHOD="GET").wsgi_request
        return _paginated_items_context(request, self.pin_list)

    def test_the_map_payload_stops_at_the_cap(self) -> None:
        self._add_pins(_MAP_PIN_LIMIT + 5)

        context = self._context()

        self.assertEqual(len(context["items_map_data"]), _MAP_PIN_LIMIT)
        self.assertTrue(context["items_map_truncated"])

    def test_a_short_list_is_not_reported_as_truncated(self) -> None:
        self._add_pins(3)

        context = self._context()

        self.assertEqual(len(context["items_map_data"]), 3)
        self.assertFalse(context["items_map_truncated"])

    def test_the_page_says_so_when_it_truncates(self) -> None:
        self._add_pins(_MAP_PIN_LIMIT + 5)

        response = self.client.get(reverse("lists.detail", kwargs={"list_slug": self.pin_list.slug}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f"Showing {_MAP_PIN_LIMIT} of {_MAP_PIN_LIMIT + 5} pins on the map")

    def test_the_rows_are_still_only_one_page(self) -> None:
        self._add_pins(_MAP_PIN_LIMIT + 5)

        context = self._context()

        self.assertEqual(len(context["items"]), 50)
        self.assertEqual(context["page_obj"].paginator.count, _MAP_PIN_LIMIT + 5)

    def _item_reads(self) -> list[str]:
        """Every statement the context builder runs against the items table."""
        self._add_pins(_MAP_PIN_LIMIT + 5)
        with CaptureQueriesContext(connection) as queries:
            self._context()
        return [
            query["sql"]
            for query in queries.captured_queries
            if "dashboard_pin_list_items" in query["sql"] and "COUNT" not in query["sql"]
        ]

    def test_the_marker_payload_is_read_for_the_chosen_rows_only(self) -> None:
        """Building the markers in Python from every item on the list would
        leave the expensive half of this exactly as it was.

        Identified by the columns rather than by the absence of a LIMIT: the
        coordinate read carries one too (`MAX_SAMPLE_POINTS`), so a test keyed on
        that would pass against either query and mean nothing.
        """
        payload_reads = [sql for sql in self._item_reads() if "description" in sql]

        self.assertTrue(payload_reads, "no statement read the marker payload; this test is measuring nothing")
        for sql in payload_reads:
            self.assertTrue(" IN (" in sql or "LIMIT 50" in sql, sql[-200:])

    def test_the_coordinate_read_carries_no_payload_columns(self) -> None:
        """The read that is not restricted to 500 rows has to be the cheap one."""
        coordinate_reads = [sql for sql in self._item_reads() if "latitude" in sql and "description" not in sql]

        self.assertTrue(
            coordinate_reads, "nothing read coordinates on their own, so the spread is not happening in SQL"
        )
        for sql in coordinate_reads:
            self.assertNotIn(
                "dashboard_reviews", sql, "the coordinate read is dragging the marker payload's joins with it"
            )

    def test_the_map_covers_the_area_rather_than_the_first_rows(self) -> None:
        """The defect the slice had: a list ordered by hand maps one cluster."""
        self._add_pins(_MAP_PIN_LIMIT + 5)
        outliers = []
        for index in range(3):
            location = baker.make(Location, latitude=f"{60 + index}.000000", longitude=f"{20 + index}.000000")
            pin = baker.make(Pin, profile=self.profile, location=location)
            baker.make(PinListItem, pin_list=self.pin_list, pin=pin)
            outliers.append(str(pin.uuid))

        plotted = {row["uuid"] for row in self._context()["items_map_data"]}

        self.assertTrue(set(outliers).issubset(plotted), "an outlying pin was dropped by the cap")


class PinListItemOrderingTests(TestCase):
    """The list's ordering has to be total, now that two slices of it are taken.

    `add_pins_to_list` numbers new items from the current row count, not from
    `max(order) + 1`, so a duplicate `order` is reachable through ordinary use.
    While `_paginated_items_context` materialized the list once and sliced in
    Python that was harmless; taking the page and the map as two separate
    queries makes it a source of disagreement.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.pin_list = baker.make(PinList, profile=self.profile, name="Tied list")

    def _pin(self) -> Pin:
        offset = next(_COORDS)
        location = baker.make(Location, latitude=f"{40 + offset * 0.001:.6f}", longitude=f"{-74 + offset * 0.001:.6f}")
        return baker.make(Pin, profile=self.profile, location=location)

    def test_a_duplicate_order_is_reachable_by_removing_then_adding(self) -> None:
        pins = [self._pin() for _ in range(5)]
        add_pins_to_list(self.pin_list, pins)
        PinListItem.objects.filter(pin_list=self.pin_list, pin=pins[2]).delete()

        add_pins_to_list(self.pin_list, [self._pin()])

        orders = list(PinListItem.objects.filter(pin_list=self.pin_list).values_list("order", flat=True))
        self.assertNotEqual(len(orders), len(set(orders)), "the premise: order is not unique within a list")

    def test_two_slices_of_the_same_list_agree_despite_the_tie(self) -> None:
        pins = [self._pin() for _ in range(5)]
        add_pins_to_list(self.pin_list, pins)
        PinListItem.objects.filter(pin_list=self.pin_list, pin=pins[2]).delete()
        add_pins_to_list(self.pin_list, [self._pin()])

        queryset = _list_items_queryset(self.pin_list)

        # A slice taken in SQL and the same slice taken in Python can only be
        # guaranteed to match under a total order.
        self.assertEqual([item.pk for item in queryset[:3]], [item.pk for item in list(queryset)[:3]])
        self.assertEqual([item.pk for item in queryset], [item.pk for item in queryset])
