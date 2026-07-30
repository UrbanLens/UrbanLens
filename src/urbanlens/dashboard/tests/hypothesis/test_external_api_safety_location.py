"""Tests for the external API's safety check-in live-location endpoint.

Live location is deliberately its own surface, excluded from the ordinary
check-in read/write endpoints - see ``views_safety_location``'s module
docstring. The properties that matter here are the same ones the WebSocket
chat consumer already enforces: a declined or removed partner must lose read
access exactly like they lose the chat group, and no one but the owner may
ever write a position, even someone who can otherwise fully read the check-in.
"""

from __future__ import annotations

import datetime

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.safety.model import SafetyCheckin, SafetyCheckinPartner, SafetyCheckinPartnerStatus, SafetyCheckinStatus
from urbanlens.dashboard.services.api_keys import generate_api_key
from urbanlens.dashboard.services.safety import create_checkin


def _bearer(raw_key: str) -> dict:
    """Build the auth header kwargs for a bearer-key request.

    Args:
        raw_key: The plaintext API key.

    Returns:
        Extra kwargs for ``self.client``.
    """
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


class _SafetyLocationTestCase(TestCase):
    """Shared setup: an owner's check-in, and a partner accepted to watch it."""

    def setUp(self) -> None:
        baker.make(User)  # first user is auto-promoted to bootstrap site admin
        self.owner_user = baker.make(User, username="explorer")
        self.owner = Profile.objects.get(user=self.owner_user)
        self.watcher_user = baker.make(User, username="watcher")
        self.watcher = Profile.objects.get(user=self.watcher_user)

        self.owner_key = self._issue_key(self.owner_user)
        self.watcher_key = self._issue_key(self.watcher_user)

        self.checkin = create_checkin(
            profile=self.owner,
            title="Quarry trip",
            checkin_by=timezone.now() + datetime.timedelta(hours=6),
            grace_period=datetime.timedelta(hours=1),
            plan_details="North rim, back by dark",
            contact_message="Please call me",
            contacts=[(None, "friend@example.com", "Friend")],
        )
        self.partner = SafetyCheckinPartner.objects.create(
            checkin=self.checkin, profile=self.watcher, invited_by=self.owner, status=SafetyCheckinPartnerStatus.ACCEPTED, accepted_at=timezone.now()
        )

        self.url = reverse("external_api:safety.checkins.location", kwargs={"checkin_slug": self.checkin.slug})

    def _issue_key(self, user: User, scopes: list[str] | None = None) -> str:
        """Issue an API key for *user*.

        Args:
            user: The key's owner.
            scopes: Scope values to grant, defaulting to safety read + write.

        Returns:
            The plaintext key.
        """
        key, raw = generate_api_key(user, "Test")
        ApiKey.objects.filter(pk=key.pk).update(scopes=scopes or [ApiKeyScope.SAFETY_READ.value, ApiKeyScope.SAFETY_WRITE.value])
        return raw


class SafetyLocationGetTests(_SafetyLocationTestCase):
    """GET is open to the owner or an accepted partner."""

    def test_owner_reads_null_fields_before_any_position_is_reported(self) -> None:
        response = self.client.get(self.url, **_bearer(self.owner_key))
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["sharing_enabled"])
        self.assertIsNone(payload["latitude"])
        self.assertIsNone(payload["longitude"])
        self.assertIsNone(payload["accuracy"])
        self.assertIsNone(payload["updated_at"])

    def test_accepted_partner_can_read(self) -> None:
        response = self.client.get(self.url, **_bearer(self.watcher_key))
        self.assertEqual(response.status_code, 200)

    def test_invited_but_not_accepted_partner_cannot_read(self) -> None:
        """Mirrors the owner-vs-partner detail endpoint: an unanswered invite grants nothing."""
        SafetyCheckinPartner.objects.filter(pk=self.partner.pk).update(status=SafetyCheckinPartnerStatus.INVITED, accepted_at=None)
        response = self.client.get(self.url, **_bearer(self.watcher_key))
        self.assertEqual(response.status_code, 404)

    def test_removed_partner_loses_read_access_immediately(self) -> None:
        """The same de-provisioning guarantee the chat consumer already gives."""
        self.assertEqual(self.client.get(self.url, **_bearer(self.watcher_key)).status_code, 200)
        SafetyCheckinPartner.objects.filter(pk=self.partner.pk).delete()
        self.assertEqual(self.client.get(self.url, **_bearer(self.watcher_key)).status_code, 404)

    def test_stranger_gets_404(self) -> None:
        raw = self._issue_key(baker.make(User, username="stranger"))
        self.assertEqual(self.client.get(self.url, **_bearer(raw)).status_code, 404)

    def test_unknown_checkin_slug_is_404(self) -> None:
        url = reverse("external_api:safety.checkins.location", kwargs={"checkin_slug": "no-such-checkin"})
        self.assertEqual(self.client.get(url, **_bearer(self.owner_key)).status_code, 404)

    def test_get_requires_safety_read(self) -> None:
        raw = self._issue_key(self.owner_user, scopes=[ApiKeyScope.PINS_READ.value])
        self.assertEqual(self.client.get(self.url, **_bearer(raw)).status_code, 403)


class SafetyLocationPatchTests(_SafetyLocationTestCase):
    """PATCH is owner-only, and can enable sharing and report a fix in one call."""

    def test_owner_enables_sharing_and_reports_a_fix_in_one_request(self) -> None:
        response = self.client.patch(
            self.url,
            {"sharing_enabled": True, "latitude": 40.0, "longitude": -105.0, "accuracy": 12.5},
            content_type="application/json",
            **_bearer(self.owner_key),
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["sharing_enabled"])
        self.assertEqual(payload["latitude"], 40.0)
        self.assertEqual(payload["longitude"], -105.0)
        self.assertEqual(payload["accuracy"], 12.5)
        self.assertIsNotNone(payload["updated_at"])

        self.checkin.refresh_from_db()
        self.assertTrue(self.checkin.live_location_sharing_enabled)
        self.assertEqual(self.checkin.live_latitude, 40.0)

    def test_disabling_sharing_clears_the_last_known_fix(self) -> None:
        self.client.patch(
            self.url, {"sharing_enabled": True, "latitude": 40.0, "longitude": -105.0}, content_type="application/json", **_bearer(self.owner_key)
        )
        response = self.client.patch(self.url, {"sharing_enabled": False}, content_type="application/json", **_bearer(self.owner_key))
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["sharing_enabled"])
        self.assertIsNone(payload["latitude"])
        self.assertIsNone(payload["longitude"])

    def test_reporting_a_fix_without_enabling_sharing_first_is_400(self) -> None:
        response = self.client.patch(self.url, {"latitude": 40.0, "longitude": -105.0}, content_type="application/json", **_bearer(self.owner_key))
        self.assertEqual(response.status_code, 400)
        self.checkin.refresh_from_db()
        self.assertIsNone(self.checkin.live_latitude)

    def test_latitude_without_longitude_is_400(self) -> None:
        response = self.client.patch(
            self.url, {"sharing_enabled": True, "latitude": 40.0}, content_type="application/json", **_bearer(self.owner_key)
        )
        self.assertEqual(response.status_code, 400)

    def test_a_fix_on_an_already_resolved_checkin_is_400(self) -> None:
        SafetyCheckin.objects.filter(pk=self.checkin.pk).update(status=SafetyCheckinStatus.FOUND_SAFE, resolved_at=timezone.now())
        response = self.client.patch(
            self.url, {"sharing_enabled": True, "latitude": 40.0, "longitude": -105.0}, content_type="application/json", **_bearer(self.owner_key)
        )
        self.assertEqual(response.status_code, 400)

    def test_accepted_partner_cannot_patch(self) -> None:
        """Read access does not imply write access - only the owner may report a position."""
        response = self.client.patch(self.url, {"sharing_enabled": True}, content_type="application/json", **_bearer(self.watcher_key))
        self.assertEqual(response.status_code, 404)
        self.checkin.refresh_from_db()
        self.assertFalse(self.checkin.live_location_sharing_enabled)

    def test_stranger_patch_is_404(self) -> None:
        raw = self._issue_key(baker.make(User, username="stranger"))
        response = self.client.patch(self.url, {"sharing_enabled": True}, content_type="application/json", **_bearer(raw))
        self.assertEqual(response.status_code, 404)

    def test_patch_requires_safety_write(self) -> None:
        raw = self._issue_key(self.owner_user, scopes=[ApiKeyScope.SAFETY_READ.value])
        response = self.client.patch(self.url, {"sharing_enabled": True}, content_type="application/json", **_bearer(raw))
        self.assertEqual(response.status_code, 403)

    def test_empty_patch_is_a_no_op(self) -> None:
        response = self.client.patch(self.url, {}, content_type="application/json", **_bearer(self.owner_key))
        self.assertEqual(response.status_code, 200)
        self.checkin.refresh_from_db()
        self.assertFalse(self.checkin.live_location_sharing_enabled)
