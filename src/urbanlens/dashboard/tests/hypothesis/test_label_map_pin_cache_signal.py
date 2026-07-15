"""Tests for the server-side Redis map pin cache invalidation on label edits.

Regression coverage for a bug where editing a label's icon/color left stale
marker icons on the main map even after the client noticed something changed
(see test_label_pin_cache_invalidation.py for that half of the fix - bumping
Pin.updated). LabelEditView/LabelCustomizeView/bulk views all mutate the
Label row via .save()/queryset without ever touching the Pin row's own
fields, so nothing previously told MapPinCache (services/map_pins/cache.py,
Redis-backed, only live when a Valkey/Redis URL is configured) to rebuild
the cached JSON for pins carrying that label - they kept serving the old
baked-in icon/color until something else happened to touch that specific
pin, or the 2-hour TTL lapsed. Label/LabelCustomization now have their own
post_save receivers in models/pin/signals.py that refresh every affected pin.
"""

from __future__ import annotations

import itertools
from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.customization.model import LabelCustomization
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.pin.model import Pin

# Location carries a unique (latitude, longitude) constraint, so every test pin
# needs its own coordinates.
_COORDS = itertools.count()


def _make_pin_with_label(profile, label) -> Pin:
    offset = next(_COORDS)
    location = baker.make("dashboard.Location", latitude=f"{40 + offset * 0.01:.6f}", longitude=f"{-74 + offset * 0.01:.6f}")
    pin = baker.make(Pin, profile=profile, location=location)
    pin.labels.add(label)
    return pin


class LabelSaveRefreshesMapPinCacheTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user: User = baker.make(User)
        self.profile = self.user.profile

    def test_editing_label_icon_refreshes_every_pin_carrying_it(self) -> None:
        label = baker.make(Label, profile=self.profile, kind="tag", name="Urbex", icon="place")
        pin = _make_pin_with_label(self.profile, label)
        other_pin = _make_pin_with_label(self.profile, label)
        offset = next(_COORDS)
        unrelated_pin = baker.make(
            Pin,
            profile=self.profile,
            location=baker.make("dashboard.Location", latitude=f"{40 + offset * 0.01:.6f}", longitude=f"{-74 + offset * 0.01:.6f}"),
        )

        with mock.patch("urbanlens.dashboard.services.map_pins.MapPinCache") as mock_cache_cls, self.captureOnCommitCallbacks(execute=True):
            label.icon = "explore"
            label.save(update_fields=["icon"])

        refreshed_pin_ids = {call.args[0].pk for call in mock_cache_cls.return_value.upsert_pin.call_args_list}
        self.assertEqual(refreshed_pin_ids, {pin.pk, other_pin.pk})
        self.assertNotIn(unrelated_pin.pk, refreshed_pin_ids)

    def test_creating_a_label_does_not_touch_the_cache(self) -> None:
        """A brand-new label isn't attached to any pin yet - nothing to refresh."""
        with mock.patch("urbanlens.dashboard.services.map_pins.MapPinCache") as mock_cache_cls, self.captureOnCommitCallbacks(execute=True):
            baker.make(Label, profile=self.profile, kind="tag", name="New Label")

        mock_cache_cls.return_value.upsert_pin.assert_not_called()


class LabelCustomizationSaveRefreshesMapPinCacheTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user: User = baker.make(User)
        self.profile = self.user.profile
        self.other_user: User = baker.make(User)
        self.other_profile = self.other_user.profile

    def test_customizing_a_global_label_refreshes_only_own_pins(self) -> None:
        global_label = baker.make(Label, profile=None, kind="tag", name="Visited", icon="check")
        own_pin = _make_pin_with_label(self.profile, global_label)
        other_profiles_pin = _make_pin_with_label(self.other_profile, global_label)

        with mock.patch("urbanlens.dashboard.services.map_pins.MapPinCache") as mock_cache_cls, self.captureOnCommitCallbacks(execute=True):
            LabelCustomization.objects.create(profile=self.profile, label=global_label, icon="star")

        refreshed_pin_ids = {call.args[0].pk for call in mock_cache_cls.return_value.upsert_pin.call_args_list}
        self.assertEqual(refreshed_pin_ids, {own_pin.pk})
        self.assertNotIn(other_profiles_pin.pk, refreshed_pin_ids)
