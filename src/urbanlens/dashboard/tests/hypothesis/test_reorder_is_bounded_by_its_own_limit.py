"""A reorder may name as many items as the thing being reordered may hold.

`PinListReorderView` and the trip activity reorder both pass whatever list was
submitted into `filter(pk__in=ids)`. A JSON body is not covered by Django's
`DATA_UPLOAD_MAX_NUMBER_FIELDS` (that guard is form-encoding only), so the only
bound was `DATA_UPLOAD_MAX_MEMORY_SIZE` - 2.5MB, or roughly 300,000 small
integers in one statement for Postgres to parse and plan on a held connection.

A fixed constant was the wrong shape here: a drag-and-drop reorder submits the
*complete* desired order, so any ceiling low enough to bound the statement is one
a large list could legitimately exceed, and silently trimming produces an order
nobody asked for. The ceiling is therefore the same limit that governs how many
items the container may hold in the first place - `max_pins_per_list` for a list,
`max_trip_activities` for a trip. A reorder naming more items than the container
can contain is not a reorder.

Both settings use `0` to mean unlimited. There the fallback is that setting's own
validator maximum, which is the largest value an administrator could ever
configure - so the bound can never refuse a reorder that a configured limit would
have allowed, while still keeping the statement finite.
"""

from __future__ import annotations

import json

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.pin_list.model import PinList, PinListItem
from urbanlens.dashboard.models.site_settings import SiteSettings
from urbanlens.dashboard.services.core.reorder_limits import UNLIMITED_LIST_FALLBACK, reorder_id_ceiling


class _ListCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.pin_list = baker.make(PinList, profile=self.profile)
        self.items = [
            baker.make(PinListItem, pin_list=self.pin_list, pin=baker.make("dashboard.Pin", profile=self.profile))
            for _ in range(3)
        ]

    def _set_max_pins(self, value: int) -> None:
        site = SiteSettings.get_current()
        site.max_pins_per_list = value
        site.save(update_fields=["max_pins_per_list"])

    def _reorder(self, ids: list[int]):
        return self.client.post(
            reverse("lists.items.reorder", kwargs={"list_slug": self.pin_list.slug}),
            data=json.dumps({"items": [{"id": value} for value in ids]}),
            content_type="application/json",
        )


class AListReorderIsBoundedByItsPinLimitTests(_ListCase):
    def test_naming_more_items_than_the_list_may_hold_is_refused(self) -> None:
        self._set_max_pins(5)

        response = self._reorder([item.pk for item in self.items] + list(range(9000, 9020)))

        self.assertEqual(response.status_code, 400)

    def test_a_reorder_within_the_limit_still_works(self) -> None:
        """The half that stops the test above passing against a view that refuses everything."""
        self._set_max_pins(5)

        response = self._reorder([item.pk for item in reversed(self.items)])

        self.assertEqual(response.status_code, 200, response.content)
        first = PinListItem.objects.get(pk=self.items[-1].pk)
        self.assertEqual(first.order, 0)

    def test_raising_the_limit_raises_the_ceiling_with_it(self) -> None:
        """The ceiling tracks the setting rather than being a constant beside it."""
        ids = [item.pk for item in self.items] + list(range(9000, 9020))
        self._set_max_pins(5)
        self.assertEqual(self._reorder(ids).status_code, 400)

        self._set_max_pins(500)

        self.assertEqual(self._reorder(ids).status_code, 200)

    def test_unlimited_falls_back_to_the_fields_own_maximum(self) -> None:
        """0 means unlimited pins; it must not mean an unlimited id list.

        Asserted on the ceiling rather than by posting a million ids, because
        that body is over ``DATA_UPLOAD_MAX_MEMORY_SIZE`` and Django refuses it
        before the view runs - a test that posted one would be measuring
        Django's guard and passing whatever this view did.
        """
        self.assertEqual(reorder_id_ceiling(0, UNLIMITED_LIST_FALLBACK), UNLIMITED_LIST_FALLBACK)
        self.assertEqual(reorder_id_ceiling(500, UNLIMITED_LIST_FALLBACK), 500)

    def test_the_body_size_guard_is_the_tighter_bound_when_unlimited(self) -> None:
        """Recorded because it is what actually stops the extreme case on default config."""
        from django.conf import settings

        self.assertLessEqual(settings.DATA_UPLOAD_MAX_MEMORY_SIZE, 5 * 1024 * 1024)

    def test_unlimited_still_allows_an_ordinary_reorder(self) -> None:
        self._set_max_pins(0)

        self.assertEqual(self._reorder([item.pk for item in reversed(self.items)]).status_code, 200)
