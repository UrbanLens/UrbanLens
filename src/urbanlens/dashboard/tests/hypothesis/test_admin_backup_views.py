"""Tests for admin-facing backup controls."""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import Permission
from django.test import RequestFactory
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.tools import ToolsIndexView


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
    """BackupStartView requires the site-admin permission and queues the Celery backup task.

    Routed through ``self.client`` (not the view's ``post()`` called directly)
    so ``PermissionRequiredMixin.dispatch()`` actually runs - calling ``post()``
    on a bare instance skips ``dispatch()`` entirely and would let a
    permission regression (e.g. the check always passing) through unnoticed.
    """

    def test_permission_gate_denies_then_admits_the_same_user(self) -> None:
        baker.make("auth.User")  # absorbs the bootstrap site-admin promotion
        user = baker.make("auth.User")
        self.client.force_login(user)
        url = reverse("tools.backup.start")

        # Real pre-condition: this user holds no permission yet.
        self.assertEqual(self.client.post(url).status_code, 403)

        # Real state-changing call, on the same user, granting the permission.
        user.user_permissions.add(Permission.objects.get(codename="view_site_admin"))

        async_result = mock.Mock(id="task-123")
        with mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task", return_value=async_result) as enqueue:
            response = self.client.post(url)

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["task_id"], "task-123")
        self.assertEqual(payload["status_url"], reverse("celery_task_status", kwargs={"task_id": "task-123"}))
        enqueue.assert_called_once()

    def test_returns_unavailable_when_enqueue_fails(self) -> None:
        baker.make("auth.User")  # absorbs the bootstrap site-admin promotion
        user = baker.make("auth.User")
        user.user_permissions.add(Permission.objects.get(codename="view_site_admin"))
        self.client.force_login(user)
        url = reverse("tools.backup.start")

        with mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task", return_value=None):
            response = self.client.post(url)

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertFalse(payload["ok"])
        self.assertIn("Unable to enqueue", payload["message"])
