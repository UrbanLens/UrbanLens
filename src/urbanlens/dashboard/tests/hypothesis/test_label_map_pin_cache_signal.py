"""Editing a label invalidates the cached map pins that draw from it - and only those.

Regression coverage for a bug where editing a label's icon/colour left stale
marker icons on the main map (see test_map_touch.py for the other half - telling
the client). LabelEditView/LabelCustomizeView/bulk views all mutate the Label row
without touching any Pin row, so nothing told MapPinCache to rebuild the cached
JSON for pins carrying that label; they kept serving the old baked-in icon until
something else happened to touch that pin, or the 2-hour TTL lapsed.

**The invalidation used to be per pin, and that was its own defect.** Rewriting
every carrying pin's payload cost a Redis round trip, two queries and a fresh
client each, inside the editing user's request - tens of thousands of them for an
account that uses one label everywhere (P102). The profile's cached set is
dropped whole instead, and the next reader rebuilds it from the database.

So what these assert is scope: which profiles' caches go, and whose do not. A
label edit reaching a stranger's cache would be a correctness bug; reaching it
per pin was an availability one.
"""

from __future__ import annotations

import itertools
from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.labels import ensure_label
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.customization.model import LabelCustomization
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.pin.model import Pin

# Location carries a unique (latitude, longitude) constraint, so every test pin
# needs its own coordinates.
_COORDS = itertools.count()


def _make_pin_with_label(profile, label) -> Pin:
    offset = next(_COORDS)
    location = baker.make(
        "dashboard.Location", latitude=f"{40 + offset * 0.01:.6f}", longitude=f"{-74 + offset * 0.01:.6f}"
    )
    pin = baker.make(Pin, profile=profile, location=location)
    pin.labels.add(label)
    return pin


def _dropped_profile_ids(mock_cache_cls: mock.MagicMock) -> set[int]:
    """Whose caches a patched `MapPinCache` was asked to drop.

    Args:
        mock_cache_cls: The patched class.

    Returns:
        Every profile id passed to `clear_for_profiles`, flattened.
    """
    return {profile_id for call in mock_cache_cls.clear_for_profiles.call_args_list for profile_id in call.args[0]}


class LabelSaveDropsCachedMapPinsTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user: User = baker.make(User)
        self.profile = self.user.profile

    def test_editing_a_label_icon_drops_the_cache_of_profiles_carrying_it(self) -> None:
        label = baker.make(Label, profile=self.profile, kind="tag", name="Urbex", icon="place")
        _make_pin_with_label(self.profile, label)
        stranger = baker.make(User).profile
        offset = next(_COORDS)
        baker.make(
            Pin,
            profile=stranger,
            location=baker.make(
                "dashboard.Location", latitude=f"{40 + offset * 0.01:.6f}", longitude=f"{-74 + offset * 0.01:.6f}"
            ),
        )

        with (
            mock.patch("urbanlens.dashboard.services.map_pins.MapPinCache") as mock_cache_cls,
            self.captureOnCommitCallbacks(execute=True),
        ):
            label.icon = "explore"
            label.save(update_fields=["icon"])

        dropped = _dropped_profile_ids(mock_cache_cls)
        self.assertEqual(dropped, {self.profile.pk})
        self.assertNotIn(stranger.pk, dropped)

    def test_it_does_not_rewrite_pins_one_at_a_time(self) -> None:
        """The shape P102 was about: the cost must not track the carrying pins."""
        label = baker.make(Label, profile=self.profile, kind="tag", name="Everywhere", icon="place")
        for _ in range(5):
            _make_pin_with_label(self.profile, label)

        with (
            mock.patch("urbanlens.dashboard.services.map_pins.MapPinCache") as mock_cache_cls,
            self.captureOnCommitCallbacks(execute=True),
        ):
            label.icon = "explore"
            label.save(update_fields=["icon"])

        mock_cache_cls.return_value.upsert_pin.assert_not_called()
        self.assertEqual(mock_cache_cls.clear_for_profiles.call_count, 1)

    def test_creating_a_label_does_not_touch_the_cache(self) -> None:
        """A brand-new label isn't attached to any pin yet - nothing to drop."""
        with (
            mock.patch("urbanlens.dashboard.services.map_pins.MapPinCache") as mock_cache_cls,
            self.captureOnCommitCallbacks(execute=True),
        ):
            baker.make(Label, profile=self.profile, kind="tag", name="New Label")

        mock_cache_cls.return_value.upsert_pin.assert_not_called()
        mock_cache_cls.clear_for_profiles.assert_not_called()


class LabelCustomizationSaveDropsCachedMapPinsTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user: User = baker.make(User)
        self.profile = self.user.profile
        self.other_user: User = baker.make(User)
        self.other_profile = self.other_user.profile

    def test_customizing_a_global_label_drops_only_the_customizers_cache(self) -> None:
        """Everyone can carry a global label; only one profile's rendering changed."""
        global_label = ensure_label(profile=None, kind="tag", name="Visited", icon="check")
        _make_pin_with_label(self.profile, global_label)
        _make_pin_with_label(self.other_profile, global_label)

        with (
            mock.patch("urbanlens.dashboard.services.map_pins.MapPinCache") as mock_cache_cls,
            self.captureOnCommitCallbacks(execute=True),
        ):
            LabelCustomization.objects.create(profile=self.profile, label=global_label, icon="star")

        dropped = _dropped_profile_ids(mock_cache_cls)
        self.assertEqual(dropped, {self.profile.pk})
        self.assertNotIn(self.other_profile.pk, dropped)
