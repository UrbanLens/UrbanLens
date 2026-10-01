"""The visit-history panel pages its pending suggestions, and public-pin region exclusion runs in PostGIS."""

from __future__ import annotations

import datetime

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.visits import _PENDING_SUGGESTIONS_PAGE_SIZE
from urbanlens.dashboard.models.public_pins.model import PublicPinCandidate, PublicPinCandidateStatus
from urbanlens.dashboard.models.visit_suggestions.model import VisitSuggestion
from urbanlens.dashboard.services.pins.public_pins import evaluate_public_pin_candidates
from urbanlens.dashboard.tests.hypothesis.test_public_pins import FAST_CONFIG, _make_eligible_location


class PendingSuggestionsArePagedTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make("auth.User")
        self.profile = self.user.profile
        self.location = baker.make("dashboard.Location", latitude="40.0", longitude="-74.0")
        self.pin = baker.make("dashboard.Pin", profile=self.profile, location=self.location)
        self.client.force_login(self.user)
        for day in range(_PENDING_SUGGESTIONS_PAGE_SIZE + 2):
            baker.make(
                VisitSuggestion,
                suggested_to=self.profile,
                location=self.location,
                latitude="40.0",
                longitude="-74.0",
                visited_at=timezone.now() - datetime.timedelta(days=day),
                from_my_activity=True,
            )

    def test_one_page_then_the_rest(self) -> None:
        url = reverse("pin.visits", args=[self.pin.slug])

        first = self.client.get(url)
        second = self.client.get(url, {"suggestions_page": 2})

        self.assertEqual(len(first.context["pending_suggestions"]), _PENDING_SUGGESTIONS_PAGE_SIZE)
        self.assertEqual(len(second.context["pending_suggestions"]), 2)
        self.assertContains(first, f"{url}?suggestions_page=2")
        shown = {s.pk for s in first.context["pending_suggestions"]} | {
            s.pk for s in second.context["pending_suggestions"]
        }
        self.assertEqual(len(shown), _PENDING_SUGGESTIONS_PAGE_SIZE + 2)


class RegionExclusionInPostgisTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make("auth.User")

    def test_passed_neighbours_are_excluded_by_the_database(self) -> None:
        for index in range(4):
            passed, _wiki, _pinners = _make_eligible_location(latitude=f"4{index}.650000", name=f"Passed Works {index}")
            PublicPinCandidate.objects.create(
                location=passed, status=PublicPinCandidateStatus.PASSED, opened_at=timezone.now()
            )
        near, _, _ = _make_eligible_location(latitude="40.695000", name="Near Neighbor Works")
        far, _, _ = _make_eligible_location(latitude="45.650000", name="Far Foundry Complex")

        with CaptureQueriesContext(connection) as ctx:
            evaluate_public_pin_candidates(FAST_CONFIG)

        self.assertFalse(PublicPinCandidate.objects.filter(location=near).exists())
        self.assertTrue(PublicPinCandidate.objects.filter(location=far).exists())
        sql = [q["sql"] for q in ctx.captured_queries]
        self.assertTrue(any("ST_DWithin" in statement for statement in sql))
        # The passed candidates' coordinates are never read out for a Python comparison.
        self.assertFalse(
            [
                s
                for s in sql
                if s.startswith(
                    'SELECT "dashboard_locations"."latitude", "dashboard_locations"."longitude" FROM "dashboard_public_pin_candidates"'
                )
            ]
        )
