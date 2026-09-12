"""The undo-history list must not read the payloads it never shows."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.undo.model import UndoAction


class TheHistoryPanelTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile: Profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)
        for index in range(3):
            baker.make(
                UndoAction,
                profile=self.profile,
                object_repr=f"Thing {index}",
                payload={"big": "x" * 5_000},
            )

    def _history_queries(self) -> list[str]:
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(reverse("undo.history"))
            self.assertEqual(response.status_code, 200)
        return [q["sql"] for q in captured.captured_queries]

    def test_the_payload_column_is_not_selected(self) -> None:
        selects = [
            sql
            for sql in self._history_queries()
            if "dashboard_undo" in sql and sql.lstrip().upper().startswith("SELECT")
        ]
        self.assertTrue(selects, "no undo query ran, so this assertion would pass against anything")
        self.assertFalse(
            [sql for sql in selects if '"payload"' in sql],
            "the history panel selected the payload column, which it never renders",
        )

    def test_the_panel_still_lists_the_entries(self) -> None:
        """Non-vacuity: deferring a column must not empty the list."""
        body = self.client.get(reverse("undo.history")).content.decode()
        for index in range(3):
            self.assertIn(f"Thing {index}", body)

    def test_reading_a_deferred_payload_is_still_possible(self) -> None:
        """Deferral is not removal - the undo itself still needs the payload."""
        action = UndoAction.objects.filter(profile=self.profile).defer("payload").first()
        assert action is not None
        self.assertEqual(action.payload["big"], "x" * 5_000)
