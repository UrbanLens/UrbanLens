"""Tests for admin-facing backup controls."""

from __future__ import annotations

from unittest import mock

from django.test import RequestFactory

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.tools import BackupStartView, ToolsIndexView


class _User:
    is_authenticated = True

    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed

    def has_perm(self, permission: str) -> bool:
        return self.allowed and permission == "dashboard.view_site_admin"


class ToolsIndexViewTests(TestCase):
    """The tools page receives a flag for site-admin-only backup tools."""

    def test_show_backup_tools_matches_permission(self) -> None:
        request = RequestFactory().get("/tools/")
        request.user = _User(allowed=True)
        with mock.patch("urbanlens.dashboard.controllers.tools.render") as render:
            ToolsIndexView().get(request)
        self.assertTrue(render.call_args.args[2]["show_backup_tools"])

    def test_hides_backup_tools_without_permission(self) -> None:
        request = RequestFactory().get("/tools/")
        request.user = _User(allowed=False)
        with mock.patch("urbanlens.dashboard.controllers.tools.render") as render:
            ToolsIndexView().get(request)
        self.assertFalse(render.call_args.args[2]["show_backup_tools"])


class BackupStartViewTests(TestCase):
    """BackupStartView queues the Celery backup task and reports failures."""

    def test_returns_accepted_with_task_id(self) -> None:
        request = RequestFactory().post("/tools/backup/start/")
        request.user = _User(allowed=True)
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
        request.user = _User(allowed=True)

        with mock.patch("urbanlens.dashboard.services.celery.safely_enqueue_task", return_value=None):
            response = BackupStartView().post(request)

        self.assertEqual(response.status_code, 503)
        self.assertIn(b"Unable to enqueue", response.content)
