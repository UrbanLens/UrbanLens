"""Regression tests for UL-239: localStorage search-history keys must be per-user.

Three of window.LocationSearchEngine.attach()'s historyKey values were
hardcoded, unscoped literals ('ul_addr_history_v1', 'ul_composer_search_history_v1',
'ul_safety_dest_history_v1') - on a shared browser, logging out and back in as a
different user reused the SAME localStorage entry, so one user's typed search
queries (which for this app routinely are place names, addresses, or - on the
safety check-in destination search - the exact place someone is about to
explore) leaked to whoever logged in next. recentPinsKey right next to
'ul_addr_history_v1' was already correctly profile-scoped, which is what made
the inconsistency obvious.

Each fixed site must render its key with the current profile's identifier
baked in, so two different users never resolve to the same key, and must also
emit a one-time cleanup of the old unscoped key so already-leaked history
sitting in a shared browser's localStorage doesn't persist forever after the
fix ships.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase


class MapAddressSearchHistoryScopingTests(TestCase):
    """The main map's address-search history key (map/index.html)."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_history_key_is_scoped_to_the_viewing_profile(self) -> None:
        body = self.client.get(reverse("map.view")).content.decode()
        self.assertIn(f"ul_addr_history_v1_{self.profile.id}", body)

    def test_stale_unscoped_key_is_cleaned_up(self) -> None:
        body = self.client.get(reverse("map.view")).content.decode()
        self.assertIn("localStorage.removeItem('ul_addr_history_v1')", body)

    def test_two_profiles_render_different_keys(self) -> None:
        other = baker.make(User)
        first_body = self.client.get(reverse("map.view")).content.decode()
        self.client.force_login(other)
        second_body = self.client.get(reverse("map.view")).content.decode()
        self.assertNotIn(f"ul_addr_history_v1_{other.profile.id}", first_body)
        self.assertNotIn(f"ul_addr_history_v1_{self.profile.id}", second_body)


class ComposerSearchHistoryScopingTests(TestCase):
    """The comment-map composer's jump-to search history key (themes/base.html)."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_history_key_is_scoped_to_the_viewing_profile(self) -> None:
        body = self.client.get(reverse("settings.view")).content.decode()
        self.assertIn(f"ul_composer_search_history_v1_{self.profile.uuid}", body)

    def test_stale_unscoped_key_is_cleaned_up(self) -> None:
        body = self.client.get(reverse("settings.view")).content.decode()
        self.assertIn("localStorage.removeItem('ul_composer_search_history_v1')", body)


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
        from urbanlens.dashboard.services.safety import create_checkin

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
