"""A page's header badges arrive with the page, not as three more requests after it.

Each signed-in page asked for its two unread counts and the safety banner as it loaded, and each of those requests paid
the whole middleware stack to run one count.
"""

from __future__ import annotations

import datetime
import re
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.notifications.model import NotificationLog

BADGES = ("msg-badge-wrap", "notif-badge-wrap", "safety-active-banner")


class TheHeaderRendersItsBadgesTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # The first account in a fresh database is promoted to admin.
        self.user = baker.make(User)
        self.client.force_login(self.user)
        messages_icon = mock.patch(
            "urbanlens.dashboard.services.messaging.direct_messages.has_used_direct_messages", return_value=True
        )
        messages_icon.start()
        self.addCleanup(messages_icon.stop)

    def _page(self) -> str:
        response = self.client.get(reverse("home.view"))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def _opening_tag(self, html: str, element_id: str) -> str:
        match = re.search(rf'<[a-z]+ id="{element_id}"[^>]*>', html)
        self.assertIsNotNone(match, f"#{element_id} is not in the page")
        assert match is not None
        return match.group(0)

    def test_the_unread_notification_count_is_in_the_page(self) -> None:
        baker.make(NotificationLog, profile=self.user.profile, _quantity=2)

        html = self._page()

        self.assertRegex(html, r'id="notif-badge-wrap"[^>]*>\s*<span class="notif-badge" id="notif-badge">2</span>')

    def test_the_messages_badge_is_in_the_page(self) -> None:
        self.assertRegex(self._page(), r'id="msg-badge-wrap"[^>]*>\s*<span class="notif-badge[^"]*" id="msg-badge">')

    def test_an_active_check_in_is_in_the_page(self) -> None:
        checkin = baker.make(
            "dashboard.SafetyCheckin",
            profile=self.user.profile,
            title="Night walk",
            checkin_by=timezone.now() + datetime.timedelta(hours=2),
            grace_period=datetime.timedelta(hours=1),
            destination_latitude="40.000000",
            destination_longitude="-74.000000",
        )

        self.assertIn(f'id="safety-active-banner-{checkin.uuid}"', self._page())

    def test_no_badge_asks_for_itself_as_the_page_loads(self) -> None:
        html = self._page()

        for element_id in BADGES:
            with self.subTest(badge=element_id):
                triggers = re.search(r'hx-trigger="([^"]*)"', self._opening_tag(html, element_id))
                self.assertNotIn("load", [part.strip() for part in triggers.group(1).split(",")] if triggers else [])

    def test_the_banner_can_be_refreshed_after_it_has_been_replaced(self) -> None:
        """A check-in page renames its check-in in place and asks the banner to follow, however often it does."""
        endpoint = reverse("safety.active_banner")
        for source, html in (("page", self._page()), ("refresh", self.client.get(endpoint).content.decode())):
            with self.subTest(source=source):
                tag = self._opening_tag(html, "safety-active-banner")
                self.assertIn(f'hx-get="{endpoint}"', tag)
                self.assertIn("safetyBannerRefresh from:body", tag)

    def test_the_polled_count_still_answers(self) -> None:
        """The messages badge is refreshed every minute and on events, from the same endpoint as before."""
        response = self.client.get(reverse("messages.unread_count"))

        self.assertContains(response, 'id="msg-badge"')
