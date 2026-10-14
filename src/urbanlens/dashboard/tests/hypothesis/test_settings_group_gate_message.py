"""The refusal for a gated plugin setting names the feature by its label, whether core or a plugin defines it."""

from __future__ import annotations

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.subscriptions import SiteFeature
from urbanlens.dashboard.plugins.points import SETTINGS_GROUPS
from urbanlens.dashboard.services.profile.profile_settings import SettingsValidationError, apply_settings_patch
from urbanlens.dashboard.services.profile.settings_groups import SettingsGroup


class GatedGroupMessageTests(TestCase):
    def setUp(self) -> None:
        baker.make("auth.User")  # the first user is promoted to site admin
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)

    def _refusal(self, feature: str) -> str:
        group = SettingsGroup(
            name="example",
            fields=frozenset({"example_flag"}),
            read=lambda _profile: {},
            write=lambda _profile, _values: None,
            feature=feature,
        )
        with SETTINGS_GROUPS.overridden([group]), self.assertRaises(SettingsValidationError) as ctx:
            apply_settings_patch(self.profile, {"example_flag": True}, user=self.user)
        return ctx.exception.errors["example_flag"]

    def test_a_core_feature_is_named_by_its_label(self) -> None:
        self.assertEqual(
            self._refusal("places"), f"The {SiteFeature.PLACES.label} feature is not enabled for your account."
        )

    def test_an_unknown_feature_falls_back_to_its_value(self) -> None:
        self.assertEqual(
            self._refusal("no_such_feature"), "The no_such_feature feature is not enabled for your account."
        )
