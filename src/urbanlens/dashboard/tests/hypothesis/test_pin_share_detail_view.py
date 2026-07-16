"""Tests for PinShareDetailView - the page a share recipient lands on from a notification.

Covers: the page renders for the recipient, 404s for anyone else, and its map
initializes (assigns window.map) unconditionally so the shared top-right
toolbar's screenshot tool never falls back to its own hardcoded default.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_share.model import PinShare

if TYPE_CHECKING:
    from django.contrib.auth.models import User

    from urbanlens.dashboard.models.profile.model import Profile


class PinShareDetailViewTests(TestCase):
    def setUp(self) -> None:
        self.sender: Profile = baker.make("auth.User").profile
        self.recipient: Profile = baker.make("auth.User").profile
        self.pin = baker.make(Pin, profile=self.sender, parent_pin=None)
        self.share = baker.make(PinShare, pin=self.pin, from_profile=self.sender, to_profile=self.recipient)

    def test_recipient_can_view(self) -> None:
        self.client.force_login(self.recipient.user)
        response = self.client.get(reverse("pin.share.detail", kwargs={"share_id": self.share.pk}))
        self.assertEqual(response.status_code, 200)

    def test_other_users_get_404(self) -> None:
        outsider: User = baker.make("auth.User")
        self.client.force_login(outsider)
        response = self.client.get(reverse("pin.share.detail", kwargs={"share_id": self.share.pk}))
        self.assertEqual(response.status_code, 404)

    def test_map_initializes_unconditionally(self) -> None:
        """window.map must be assigned even when there's no early-return path
        skipped - regression guard for the screenshot-tool-defaults-to-Manhattan
        bug class (the map used to only initialize when coordinates existed)."""
        self.client.force_login(self.recipient.user)
        response = self.client.get(reverse("pin.share.detail", kwargs={"share_id": self.share.pk}))
        self.assertContains(response, "window.map = map;")
        self.assertContains(response, "L.map('shared-pin-map'")
