"""Tests for the global-search HTTP endpoints (panel, commit, history delete)."""

from __future__ import annotations

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.search_history import SearchHistory


class SearchPanelViewTests(TestCase):
    """GET search/panel/ renders suggestions or results and requires login."""

    def setUp(self):
        self.user = baker.make("auth.User")
        self.user.set_password("pw")
        self.user.save()
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_requires_login(self):
        self.client.logout()
        response = self.client.get(reverse("search.panel"))
        self.assertEqual(response.status_code, 302)

    def test_blank_query_shows_recent_searches_and_hints(self):
        SearchHistory.objects.record(self.profile, "old asylum")
        response = self.client.get(reverse("search.panel"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "old asylum")
        self.assertContains(response, "Try searching for")

    def test_query_renders_results(self):
        baker.make("dashboard.Pin", profile=self.profile, name="Waterworks Ruin")
        response = self.client.get(reverse("search.panel"), {"q": "waterworks"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Waterworks Ruin")

    def test_no_results_state(self):
        response = self.client.get(reverse("search.panel"), {"q": "zzzzqqqq"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No results")


class SearchCommitViewTests(TestCase):
    """POST search/commit/ records history exactly once per distinct query."""

    def setUp(self):
        self.user = baker.make("auth.User")
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_commit_records_history(self):
        response = self.client.post(reverse("search.commit"), {"q": "flooded quarry"})
        self.assertEqual(response.status_code, 204)
        self.assertTrue(SearchHistory.objects.filter(profile=self.profile, query="flooded quarry").exists())

    def test_blank_commit_records_nothing(self):
        self.client.post(reverse("search.commit"), {"q": "  "})
        self.assertEqual(SearchHistory.objects.for_profile(self.profile).count(), 0)


class SearchHistoryDeleteViewTests(TestCase):
    """POST search/history/delete/ removes one row or all, scoped to the owner."""

    def setUp(self):
        self.user = baker.make("auth.User")
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.row = SearchHistory.objects.record(self.profile, "power plant")

    def test_delete_single_row(self):
        response = self.client.post(reverse("search.history.delete"), {"history_id": str(self.row.pk)})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(SearchHistory.objects.filter(pk=self.row.pk).exists())

    def test_delete_all(self):
        SearchHistory.objects.record(self.profile, "substation")
        self.client.post(reverse("search.history.delete"), {"all": "1"})
        self.assertEqual(SearchHistory.objects.for_profile(self.profile).count(), 0)

    def test_cannot_delete_another_users_history(self):
        stranger_row = SearchHistory.objects.record(baker.make("auth.User").profile, "their search")
        self.client.post(reverse("search.history.delete"), {"history_id": str(stranger_row.pk)})
        self.assertTrue(SearchHistory.objects.filter(pk=stranger_row.pk).exists())
