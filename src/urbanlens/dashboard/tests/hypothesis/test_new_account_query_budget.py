"""
Creating an account and logging in cost a fixed number of queries.

Seeding a new profile's default labels queued their REData definitions with one query per label for its parents: 34
extra queries on every signup, and on every ``baker.make(User)`` in the suite, about a fifth of its run time. The
ceilings below are the measured counts with REData configured, so the label sync is on the path.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest import mock

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY, KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.labels.signals import CATEGORY_HIERARCHY, DEFAULT_CATEGORIES, DEFAULT_TAGS
from urbanlens.dashboard.services.labels import redata_suggestions
from urbanlens.UrbanLens.settings.app import settings

#: 58 before the label sync read every label's parents in one query.
CREATE_USER_CEILING = 25
FORCE_LOGIN_CEILING = 29
PASSWORD_LOGIN_CEILING = 31


@contextmanager
def _redata_configured():
    with (
        mock.patch.object(settings, "redata_api_url", "https://redata.example.test"),
        mock.patch.object(settings, "redata_api_key", "test-key"),
    ):
        yield


class NewAccountQueryBudgetTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion, which only the first account pays

    def _count(self, action) -> int:
        with _redata_configured(), CaptureQueriesContext(connection) as queries:
            action()
        return len(queries)

    def test_creating_a_user_stays_within_its_query_budget(self) -> None:
        count = self._count(lambda: baker.make(User))

        self.assertLessEqual(count, CREATE_USER_CEILING)

    def test_the_seeded_labels_are_synced_without_a_query_each(self) -> None:
        """Anti-vacuity: the profile really is seeded with the labels the sync walks, and REData would receive them."""
        user = baker.make(User)
        seeded = list(Label.objects.for_profile(user.profile).filter(kind__in=(KIND_TAG, KIND_CATEGORY)))

        self.assertEqual(len(seeded), len(DEFAULT_CATEGORIES) + len(DEFAULT_TAGS))
        with _redata_configured(), self.assertNumQueries(1):
            definitions = redata_suggestions._label_definitions(seeded, is_active=True)
        self.assertEqual(sum(bool(definition["parent_ids"]) for definition in definitions), len(CATEGORY_HIERARCHY))

    def test_force_login_stays_within_its_query_budget(self) -> None:
        user = baker.make(User)

        count = self._count(lambda: self.client.force_login(user))

        self.assertLessEqual(count, FORCE_LOGIN_CEILING)

    def test_a_password_login_stays_within_its_query_budget(self) -> None:
        user = baker.make(User, username="budget-explorer", is_active=True)
        user.set_password("correct horse battery staple")
        user.save()

        count = self._count(
            lambda: self.assertTrue(
                self.client.login(username="budget-explorer", password="correct horse battery staple")
            )
        )

        self.assertLessEqual(count, PASSWORD_LOGIN_CEILING)
