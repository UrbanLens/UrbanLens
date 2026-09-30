"""An archived check-in's owner gets what the browser needs to unlock it: the E2EE client and its configuration.

The Unlock button used to call ``UrbanLensE2EE.decryptSafetyArchive`` on a page that never loaded ``e2ee.js`` or
called ``init()``, so it did nothing.
"""

from __future__ import annotations

import base64
import datetime
import os

from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.e2ee.key_bundle import MessagingKeyBundle
from urbanlens.dashboard.models.safety.model import (
    SafetyCheckinPartner,
    SafetyCheckinPartnerStatus,
    SafetyCheckinStatus,
)
from urbanlens.dashboard.services.visits.safety import archive_checkin


class ArchivedCheckinUnlockPageTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.owner = baker.make("auth.User").profile
        MessagingKeyBundle.objects.create(
            profile=self.owner,
            public_key=base64.b64encode(os.urandom(32)).decode(),
            recovery_wrapped_secret=base64.b64encode(os.urandom(72)).decode(),
        )
        self.checkin = baker.make(
            "dashboard.SafetyCheckin",
            profile=self.owner,
            title="Night walk",
            checkin_by=timezone.now() - datetime.timedelta(hours=1),
            grace_period=datetime.timedelta(hours=1),
            status=SafetyCheckinStatus.FOUND_SAFE,
            resolved_at=timezone.now(),
            resolved_by_label="you",
        )
        archive_checkin(self.checkin)
        self.checkin.refresh_from_db()
        self.url = reverse("safety.checkin.detail", args=[self.checkin.slug or self.checkin.uuid])

    def test_the_owner_gets_the_e2ee_client_and_its_configuration(self) -> None:
        self.client.force_login(self.owner.user)
        response = self.client.get(self.url)
        self.assertContains(response, 'id="safety-archive-unlock-btn"')
        self.assertContains(response, "dashboard/js/e2ee.js")
        self.assertContains(response, f'data-self-slug="{self.owner.ensure_slug()}"')
        self.assertContains(response, f'data-url-login-params="{reverse("e2ee.login_params")}"')
        self.assertContains(response, f'data-url-keys="{reverse("e2ee.keys")}"')

    def test_a_partner_gets_neither(self) -> None:
        partner = baker.make("auth.User").profile
        SafetyCheckinPartner.objects.create(
            checkin=self.checkin, profile=partner, invited_by=self.owner, status=SafetyCheckinPartnerStatus.ACCEPTED
        )
        self.client.force_login(partner.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'id="safety-archive-unlock-btn"')
        self.assertNotContains(response, "data-self-slug=")
