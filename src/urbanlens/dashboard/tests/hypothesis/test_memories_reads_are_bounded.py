"""A memory source reads no more rows than its limit, and the timeline API pages with a cursor.

``islice`` over ``for row in queryset`` bounded the merged list but not the reads: iterating a queryset
loads every row before yielding the first, so a limit of 51 still hydrated every visit, route and photo
in the requested range.
"""

from __future__ import annotations

import datetime
import re

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.visits.model import PinVisit
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.services.memories.aggregator import get_memory_events


class _Case(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.pin = baker.make(Pin, profile=self.profile, location=baker.make(Location))
        self.today = timezone.now().date()
        self.seeded = 0

    def seed_visits(self, count: int) -> None:
        for _ in range(count):
            self.seeded += 1
            baker.make(PinVisit, pin=self.pin, visited_at=timezone.now() - datetime.timedelta(hours=self.seeded))


class SourceReadsAreBoundedTests(_Case):
    def _visit_reads(self, limit: int) -> list[str]:
        with CaptureQueriesContext(connection) as ctx:
            get_memory_events(self.profile, self.today - datetime.timedelta(days=30), self.today, limit=limit)
        return [
            q["sql"]
            for q in ctx.captured_queries
            if '"dashboard_pin_visits"' in q["sql"] or "pinvisit" in q["sql"].lower()
        ]

    def test_the_visit_read_carries_the_limit(self) -> None:
        self.seed_visits(12)

        reads = self._visit_reads(limit=5)

        self.assertEqual(len(reads), 1, reads)
        self.assertRegex(reads[0], r"LIMIT 5\b")

    def test_query_count_does_not_grow_with_rows_in_range(self) -> None:
        self.seed_visits(3)
        with CaptureQueriesContext(connection) as small:
            get_memory_events(self.profile, self.today - datetime.timedelta(days=30), self.today, limit=5)
        self.seed_visits(40)
        with CaptureQueriesContext(connection) as large:
            events = get_memory_events(self.profile, self.today - datetime.timedelta(days=30), self.today, limit=5)

        self.assertEqual(len(events), 5)
        self.assertEqual(len(large.captured_queries), len(small.captured_queries))


class TimelineCursorTests(_Case):
    def setUp(self) -> None:
        super().setUp()
        api_key, self.raw_key = generate_api_key(self.user, "Mobile")
        api_key.scopes = [ApiKeyScope.PHOTOS_READ.value]
        api_key.save(update_fields=["scopes"])

    def _get(self, url: str, **params):
        return self.client.get(url, params, HTTP_AUTHORIZATION=f"Bearer {self.raw_key}")

    def test_the_next_link_walks_every_event_once(self) -> None:
        self.seed_visits(7)

        url = reverse("external_api:memories.timeline")
        seen, pages = [], 0
        response = self._get(url, limit=3)
        while True:
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertIsNone(body["count"])
            self.assertIsNone(body["previous"])
            seen.extend(row["extra"]["visit_id"] for row in body["results"])
            pages += 1
            if not body["next"]:
                break
            self.assertIn("limit=3", body["next"])
            response = self.client.get(body["next"], HTTP_AUTHORIZATION=f"Bearer {self.raw_key}")

        self.assertEqual(pages, 3)
        self.assertEqual(len(seen), 7)
        self.assertEqual(len(set(seen)), 7)

    def test_limit_is_capped(self) -> None:
        response = self._get(reverse("external_api:memories.timeline"), limit=1000)

        self.assertEqual(response.status_code, 400)

    def test_a_wide_range_costs_the_same_as_a_narrow_one(self) -> None:
        url = reverse("external_api:memories.timeline")
        self.seed_visits(3)
        self._get(url)
        with CaptureQueriesContext(connection) as small:
            self._get(url, start="2000-01-01", limit=5)
        self.seed_visits(40)
        with CaptureQueriesContext(connection) as large:
            response = self._get(url, start="2000-01-01", limit=5)

        self.assertEqual(len(response.json()["results"]), 5)
        shape = [re.sub(r"\d+", "N", q["sql"]) for q in large.captured_queries]
        self.assertEqual(shape, [re.sub(r"\d+", "N", q["sql"]) for q in small.captured_queries])
