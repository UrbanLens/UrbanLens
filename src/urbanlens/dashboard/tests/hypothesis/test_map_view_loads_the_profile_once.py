"""A map request selects the viewer's profile row once.

Measured: the row was selected three times - the middleware's descriptor read, ``view_map``'s own
``get_or_create``, and the assistant template tag's. ``get_or_create`` selects whether or not the
descriptor cache is already populated, so each extra call site pays again for a row the request holds.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase


class MapViewLoadsTheProfileOnceTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin, which grants every feature
        self.user = baker.make(User)

    def test_the_profile_row_is_selected_once(self) -> None:
        self.client.force_login(self.user)

        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(reverse("map.view"))

        self.assertEqual(response.status_code, 200)
        selects = [
            query["sql"]
            for query in captured.captured_queries
            if query["sql"].startswith('SELECT "dashboard_profiles"')
        ]
        self.assertEqual(len(selects), 1, f"selected the profile {len(selects)} times")
