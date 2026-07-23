"""Smoke tests for the Settings > Connections "Browser Permissions" section.

Covers only server-rendered markup - the actual permission status/prompt
logic lives client-side in shared/permissions-client.ts (no server round
trip), so there's nothing else here for Django tests to exercise.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase


class BrowserPermissionsSectionTests(TestCase):
    def setUp(self) -> None:
        self.user = baker.make(User)
        self.client.force_login(self.user)

    def test_page_renders_successfully(self) -> None:
        response = self.client.get(reverse("settings.view"))
        self.assertEqual(response.status_code, 200)

    def test_section_and_both_permission_cards_present(self) -> None:
        response = self.client.get(reverse("settings.view"))
        self.assertContains(response, "Browser Permissions")
        self.assertContains(response, 'data-permission="location"')
        self.assertContains(response, 'data-permission="notifications"')

    def test_permissions_bundle_is_included(self) -> None:
        response = self.client.get(reverse("settings.view"))
        self.assertContains(response, "dashboard/js/permissions.js")
