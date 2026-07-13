"""Tests for admin-facing backup controls."""

from __future__ import annotations

from unittest import mock

from django.test import RequestFactory
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.tools import BackupStartView, ToolsIndexView


def _user(allowed: bool = True):
    """A real auth.User (so FK lookups in the view work) with has_perm stubbed."""
    user = baker.make("auth.User")
    user.has_perm = lambda permission: allowed and permission == "dashboard.view_site_admin"
    return user


class ToolsIndexViewTests(TestCase):
    """The tools page receives a flag for site-admin-only backup tools."""

    def test_show_backup_tools_matches_permission(self) -> None:
        request = RequestFactory().get("/tools/")
        request.user = _user(allowed=True)
        with mock.patch("urbanlens.dashboard.controllers.tools.render") as render:
            ToolsIndexView().get(request)
        self.assertTrue(render.call_args.args[2]["show_backup_tools"])

    def test_hides_backup_tools_without_permission(self) -> None:
        request = RequestFactory().get("/tools/")
        request.user = _user(allowed=False)
        with mock.patch("urbanlens.dashboard.controllers.tools.render") as render:
            ToolsIndexView().get(request)
        self.assertFalse(render.call_args.args[2]["show_backup_tools"])


class BackupStartViewTests(TestCase):
    """BackupStartView queues the Celery backup task and reports failures."""

    def test_returns_accepted_with_task_id(self) -> None:
        request = RequestFactory().post("/tools/backup/start/")
        request.user = _user(allowed=True)
        async_result = mock.Mock(id="task-123")

        with (
            mock.patch("urbanlens.dashboard.services.celery.safely_enqueue_task", return_value=async_result) as enqueue,
            mock.patch("urbanlens.dashboard.controllers.tools.reverse", return_value="/tasks/task-123/status/"),
        ):
            response = BackupStartView().post(request)

        self.assertEqual(response.status_code, 202)
        self.assertIn(b"task-123", response.content)
        enqueue.assert_called_once()

    def test_returns_unavailable_when_enqueue_fails(self) -> None:
        request = RequestFactory().post("/tools/backup/start/")
        request.user = _user(allowed=True)

        with mock.patch("urbanlens.dashboard.services.celery.safely_enqueue_task", return_value=None):
            response = BackupStartView().post(request)

        self.assertEqual(response.status_code, 503)
        self.assertIn(b"Unable to enqueue", response.content)
