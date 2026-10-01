"""The trips list's sort control and create dialog work from markup the core bundle reads, not page scripts."""

from __future__ import annotations

import html as html_lib
import json
import re

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile


class TripListMarkupTests(TestCase):
    def setUp(self) -> None:
        user = baker.make(User)
        Profile.objects.filter(user=user).update(welcome_onboarding_complete=True, profile_setup_complete=True)
        self.client.force_login(user)

    def _page(self, url: str) -> str:
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, url)
        return response.content.decode()

    def test_each_sort_option_leads_to_the_list_sorted_that_way(self) -> None:
        page = self._page(reverse("trips.list"))
        select = re.search(r'<select id="trip-sort-select"[^>]*\bdata-navigate\b[^>]*>(.*?)</select>', page, re.DOTALL)
        assert select is not None
        options = [html_lib.unescape(value) for value in re.findall(r'<option value="([^"]+)"', select.group(1))]
        self.assertEqual(len(options), 4)
        for url in options:
            self.assertTrue(url.startswith(reverse("trips.list") + "?"), url)
            landed = self._page(url)
            self.assertRegex(landed, rf'<option value="{re.escape(html_lib.escape(url))}"\s+selected>')

    def test_the_create_dialog_suggests_one_of_its_own_ideas(self) -> None:
        page = self._page(reverse("trips.list"))
        island = re.search(r'<script id="trip-name-ideas" type="application/json">(.*?)</script>', page, re.DOTALL)
        assert island is not None
        ideas = json.loads(island.group(1))
        placeholder = re.search(r'id="trip-name"\s+placeholder="e\.g\. ([^"]+)"', page)
        assert placeholder is not None
        self.assertIn(html_lib.unescape(placeholder.group(1)), ideas)
        self.assertIn('data-placeholder-ideas="trip-name-ideas"', page)
        self.assertIn('data-reveal="trip-create-dates-row"', page)
