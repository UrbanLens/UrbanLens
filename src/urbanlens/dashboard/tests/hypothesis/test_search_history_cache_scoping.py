"""Regression tests for UL-239: localStorage search-history keys must be per-user."""

from __future__ import annotations

from pathlib import Path

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.inline_scripts import rendered_config
from urbanlens.core.tests.testcase import TestCase

#: P92 moved this from an inline <script> into a bundled TS entry (P124) - the key-scoping
#: code itself is static now, so it is checked against the source file, not a response body.
_MAP_SCRIPT_SOURCE = (Path(__file__).resolve().parents[3] / "dashboard/frontend/ts/entries/map-page.ts").read_text()

#: Never migrated off a classic <script src> - still checkable as a plain static file.
_COMPOSER_SCRIPT_SOURCE = (
    Path(__file__).resolve().parents[3] / "dashboard/frontend/static/js/comment-map.js"
).read_text()


class MapAddressSearchHistoryScopingTests(TestCase):
    """The main map's address-search history key (map/index.html)."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_history_key_is_scoped_to_the_viewing_profile(self) -> None:
        # The script builds the key from a variable, not a literal - the actual per-viewer
        # value only exists in the page's own config element.
        self.assertIn('historyKey: "ul_addr_history_v1_" + _PROFILE_UUID', _MAP_SCRIPT_SOURCE)
        config = rendered_config(self.client.get(reverse("map.view")).content, "map-page-config")
        assert config is not None, "map-page-config json_script element not found in response"
        self.assertEqual(config["profileUuid"], str(self.profile.uuid))

    def test_stale_unscoped_key_is_cleaned_up(self) -> None:
        self.assertIn('localStorage.removeItem("ul_addr_history_v1")', _MAP_SCRIPT_SOURCE)

    def test_two_profiles_render_different_keys(self) -> None:
        other = baker.make(User)
        first_config = rendered_config(self.client.get(reverse("map.view")).content, "map-page-config")
        self.client.force_login(other)
        second_config = rendered_config(self.client.get(reverse("map.view")).content, "map-page-config")
        assert first_config is not None, "map-page-config json_script element not found in response"
        assert second_config is not None, "map-page-config json_script element not found in response"
        self.assertEqual(first_config["profileUuid"], str(self.profile.uuid))
        self.assertEqual(second_config["profileUuid"], str(other.profile.uuid))
        self.assertNotEqual(first_config["profileUuid"], second_config["profileUuid"])


class ComposerSearchHistoryScopingTests(TestCase):
    """The comment-map composer's jump-to search history key (themes/base.html)."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_history_key_is_scoped_to_the_viewing_profile(self) -> None:
        self.assertIn(
            "historyKey: 'ul_composer_search_history_v1_' + COMMENT_MAP_CFG.profileUuid", _COMPOSER_SCRIPT_SOURCE
        )
        config = rendered_config(self.client.get(reverse("settings.view")).content, "comment-map-config")
        assert config is not None, "comment-map-config json_script element not found in response"
        self.assertEqual(config["profileUuid"], str(self.profile.uuid))

    def test_stale_unscoped_key_is_cleaned_up(self) -> None:
        self.assertIn("localStorage.removeItem('ul_composer_search_history_v1')", _COMPOSER_SCRIPT_SOURCE)


class SafetyDestinationSearchHistoryScopingTests(TestCase):
    """The safety check-in destination search history key (_safety_map_script.html) -
    the most sensitive of the three, since it records exactly which places a user
    was about to explore."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def _create_checkin_response_body(self) -> str:
        import datetime

        from django.utils import timezone

        from urbanlens.dashboard.models.markup.model import MarkupMap
        from urbanlens.dashboard.services.visits.safety import create_checkin

        checkin = create_checkin(
            profile=self.profile,
            title="Test checkin",
            checkin_by=timezone.now() + datetime.timedelta(hours=2),
            grace_period=datetime.timedelta(hours=1),
        )
        # pages/safety/detail.html reverses several markup_map.* URLs off
        # checkin.markup_map.uuid unconditionally, so a real one is required
        # for the owner's page to render at all - unrelated to what this test
        # actually checks (the search-history key), but load-bearing for setup.
        checkin.markup_map = baker.make(MarkupMap, profile=self.profile)
        checkin.save(update_fields=["markup_map", "updated"])
        url = reverse("safety.checkin.detail", kwargs={"checkin_slug": checkin.slug or str(checkin.uuid)})
        return self.client.get(url).content.decode()

    def test_history_key_is_scoped_to_the_viewing_profile(self) -> None:
        body = self._create_checkin_response_body()
        self.assertIn(f"ul_safety_dest_history_v1_{self.profile.uuid}", body)

    def test_stale_unscoped_key_is_cleaned_up(self) -> None:
        body = self._create_checkin_response_body()
        self.assertIn("localStorage.removeItem('ul_safety_dest_history_v1')", body)
