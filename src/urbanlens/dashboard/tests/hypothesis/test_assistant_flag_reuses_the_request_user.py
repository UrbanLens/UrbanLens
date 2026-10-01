"""``assistant_enabled_flag`` must not refetch the profile and user the request is already holding.

Measured on a map request: the tag issued 4 of 24 queries and 28% of the SQL. ``get_or_create`` always selects,
ignoring the descriptor cache the middleware filled, and the profile it returns carries no cached ``user``, so
``profile.user`` refetches the user and lands a cold permission cache that costs two more queries.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.templatetags.dashboard_tags import assistant_enabled_flag


class AssistantFlagReusesTheRequestUserTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin, which grants every feature
        self.user = baker.make(User)

    def _sql_the_tag_issues(self) -> str:
        """The tag's own SQL, after a request has already loaded the profile and checked a permission."""
        _ = self.user.profile
        self.user.has_perm("dashboard.view_site_admin")

        with CaptureQueriesContext(connection) as captured:
            assistant_enabled_flag(self.user)
        return " ".join(query["sql"] for query in captured.captured_queries)

    def test_it_does_not_refetch_the_profile(self) -> None:
        self.assertNotIn('FROM "dashboard_profiles"', self._sql_the_tag_issues())

    def test_it_does_not_refetch_the_user(self) -> None:
        self.assertNotIn('FROM "auth_user" ', self._sql_the_tag_issues())

    def test_it_does_not_recheck_permissions(self) -> None:
        self.assertNotIn('FROM "auth_permission"', self._sql_the_tag_issues())
