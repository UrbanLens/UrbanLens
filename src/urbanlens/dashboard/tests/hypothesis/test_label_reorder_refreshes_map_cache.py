"""Reordering labels invalidates the cached map pin payload.

Which label supplies a pin's map icon/colour is decided by label ``order``:
``_ordered_location_labels`` sorts by ``-order`` and ``_winning_display_label``
takes the first entry carrying an icon. So reordering two icon-bearing labels
changes what a pin looks like on the map without touching the pin at all.

``refresh_map_pin_cache_for_label`` exists for exactly this hazard - its docstring
notes that editing a label never touches ``Pin.labels.through``, so nothing else
would invalidate the Redis payload and affected pins "keep serving the old
baked-in icon/color". But it is a ``post_save`` receiver, and reorder writes through
``queryset.update()`` (now ``bulk_update``), neither of which fires it. The one
write that changes ``order`` was the one write that skipped the invalidation.

This asserts the invalidation *contract* - that reorder asks for the affected
labels' pins to be refreshed - rather than reading back a Redis payload. The cache
needs a live client, and this suite's network guard permits localhost only, so an
end-to-end round-trip is not available here. The refresh helper's own behaviour is
covered where it is defined.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.meta import KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.profile.model import Profile

# Patched where the controller looks it up, not where it is defined.
REFRESH = "urbanlens.dashboard.controllers.labels.refresh_map_pin_cache_for_label_ids"


class LabelReorderRefreshesMapCacheTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)
        self.url = reverse("label.reorder", kwargs={"label_kind": KIND_TAG})

        self.low = baker.make(Label, profile=self.profile, kind=KIND_TAG, name="Low", order=1)
        self.high = baker.make(Label, profile=self.profile, kind=KIND_TAG, name="High", order=2)

    def _reorder(self, ids: list[int]):
        return self.client.post(self.url, data=json.dumps({"tag_ids": ids}), content_type="application/json")

    def test_reorder_requests_a_cache_refresh_for_the_reordered_labels(self) -> None:
        with patch(REFRESH) as refresh:
            self.assertEqual(self._reorder([self.low.pk, self.high.pk]).status_code, 200)

        refresh.assert_called_once()
        self.assertEqual(sorted(refresh.call_args.args[0]), sorted([self.low.pk, self.high.pk]))

    def test_labels_the_caller_does_not_own_are_not_refreshed(self) -> None:
        """The refresh list has to come from the rows actually written, not the posted ids."""
        stranger = baker.make("auth.User")
        theirs = baker.make(Label, profile=Profile.objects.get(user=stranger), kind=KIND_TAG, name="Theirs")

        # low must actually move, or it would be filtered out as unchanged and the
        # assertion would pass for the wrong reason.
        with patch(REFRESH) as refresh:
            self.assertEqual(self._reorder([self.low.pk, theirs.pk]).status_code, 200)

        self.assertEqual(refresh.call_args.args[0], [self.low.pk])

    def test_a_reorder_that_writes_nothing_does_not_refresh(self) -> None:
        with patch(REFRESH) as refresh:
            self.assertEqual(self._reorder([]).status_code, 200)

        refresh.assert_not_called()

    def test_resending_the_existing_order_refreshes_nothing(self) -> None:
        """Refreshing costs work per pin carrying the label, so an unchanged order must be a no-op."""
        with patch(REFRESH) as refresh:
            self.assertEqual(self._reorder([self.high.pk, self.low.pk]).status_code, 200)

        refresh.assert_not_called()
