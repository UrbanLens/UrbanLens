"""
Creating an account and logging in cost a fixed number of queries.

Seeding a new profile's default labels once cost a query per label on every signup, and on every ``baker.make(User)``
in the suite, about a fifth of its run time. A plugin that follows labels pays its own cost in its own tests.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY, KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.labels.signals import DEFAULT_CATEGORIES, DEFAULT_TAGS

#: 58 before the label sync read every label's parents in one query.
CREATE_USER_CEILING = 25
FORCE_LOGIN_CEILING = 29
PASSWORD_LOGIN_CEILING = 31


class NewAccountQueryBudgetTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion, which only the first account pays

    def _count(self, action) -> int:
        with CaptureQueriesContext(connection) as queries:
            action()
        return len(queries)

    def test_creating_a_user_stays_within_its_query_budget(self) -> None:
        count = self._count(lambda: baker.make(User))

        self.assertLessEqual(count, CREATE_USER_CEILING)

    def test_the_profile_is_seeded_with_the_default_labels(self) -> None:
        """Anti-vacuity: the budget above covers seeding a profile's whole taxonomy."""
        user = baker.make(User)
        seeded = Label.objects.for_profile(user.profile).filter(kind__in=(KIND_TAG, KIND_CATEGORY))

        self.assertEqual(seeded.count(), len(DEFAULT_CATEGORIES) + len(DEFAULT_TAGS))

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
