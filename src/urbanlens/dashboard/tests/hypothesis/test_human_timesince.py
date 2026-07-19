"""Tests for the human_timesince template filter (never say "0 minutes ago")."""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.templatetags.dashboard_tags import human_timesince


class HumanTimesinceTests(SimpleTestCase):
    def test_just_now_for_sub_minute_timestamps(self) -> None:
        self.assertEqual(human_timesince(timezone.now() - timedelta(seconds=5)), "just now")

    def test_just_now_for_exact_now(self) -> None:
        self.assertEqual(human_timesince(timezone.now()), "just now")

    def test_appends_ago_for_older_timestamps(self) -> None:
        result = human_timesince(timezone.now() - timedelta(hours=2))
        self.assertTrue(result.endswith(" ago"))
        self.assertNotEqual(result, "just now")
