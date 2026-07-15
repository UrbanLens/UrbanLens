"""Tests for the Tools page subnav (Data | Find Pins | Account).

Regression coverage for the tools page being reorganized from one flat grid
of cards into three subnav-switched sections - covers that all the original
cards are still present and reachable, and that the section panels/tabs
exist with matching ids for the client-side tab-switching script to hook into.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase


class ToolsPageSubnavTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # first user is auto-promoted to bootstrap site admin
        self.user: User = baker.make(User)
        self.client.force_login(self.user)

    def test_page_renders_with_subnav_and_all_three_panels(self) -> None:
        response = self.client.get(reverse("tools.index"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-section="data"')
        self.assertContains(response, 'data-section="find"')
        self.assertContains(response, 'data-section="account"')
        self.assertContains(response, 'id="panel-data"')
        self.assertContains(response, 'id="panel-find"')
        self.assertContains(response, 'id="panel-account"')

    def test_data_panel_contains_export_and_import_cards(self) -> None:
        response = self.client.get(reverse("tools.index"))
        content = response.content.decode()
        data_panel = content.split('id="panel-data"')[1].split('id="panel-find"')[0]
        self.assertIn('id="export-card"', data_panel)
        self.assertIn('id="import-card"', data_panel)

    def test_find_panel_contains_photo_and_immich_scan_cards(self) -> None:
        response = self.client.get(reverse("tools.index"))
        content = response.content.decode()
        find_panel = content.split('id="panel-find"')[1].split('id="panel-account"')[0]
        self.assertIn('id="photo-scan-card"', find_panel)
        self.assertIn('id="immich-scan-card"', find_panel)

    def test_account_panel_contains_invite_friend_card(self) -> None:
        response = self.client.get(reverse("tools.index"))
        content = response.content.decode()
        account_panel = content.split('id="panel-account"')[1]
        self.assertIn('id="invite-friend-card"', account_panel)

    def test_admin_tools_card_only_shown_to_admins(self) -> None:
        response = self.client.get(reverse("tools.index"))
        self.assertNotContains(response, 'id="admin-tools-card"')

    def test_admin_tools_card_shown_to_site_admins(self) -> None:
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])
        response = self.client.get(reverse("tools.index"))
        self.assertContains(response, 'id="admin-tools-card"')
