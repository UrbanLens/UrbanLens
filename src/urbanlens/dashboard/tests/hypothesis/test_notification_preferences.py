"""Tests for gating WhatsApp/SMS notification channels on having a number connected.

Regression coverage for a bug where WhatsApp/SMS toggles in Settings >
Notifications were always clickable, even for a profile with no WhatsApp
number or phone number on file to actually deliver to.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.notifications.model import NotificationPreference


class NotificationChannelGatingTests(TestCase):
    def setUp(self) -> None:
        self.user: User = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_whatsapp_toggle_is_rejected_without_a_whatsapp_number(self) -> None:
        self.profile.whatsapp_number = ""
        self.profile.save(update_fields=["whatsapp_number"])

        response = self.client.post(reverse("notifications.preferences"), {"message__site": "1", "message_whatsapp": "1"})

        self.assertEqual(response.status_code, 200)
        prefs = NotificationPreference.objects.get(profile=self.profile)
        self.assertFalse(prefs.message_whatsapp)

    def test_sms_toggle_is_rejected_without_a_phone_number(self) -> None:
        self.profile.phone_number = ""
        self.profile.save(update_fields=["phone_number"])

        response = self.client.post(reverse("notifications.preferences"), {"message__site": "1", "message_sms": "1"})

        self.assertEqual(response.status_code, 200)
        prefs = NotificationPreference.objects.get(profile=self.profile)
        self.assertFalse(prefs.message_sms)

    def test_whatsapp_toggle_is_accepted_once_a_whatsapp_number_is_connected(self) -> None:
        self.profile.whatsapp_number = "+15550001111"
        self.profile.save(update_fields=["whatsapp_number"])

        response = self.client.post(reverse("notifications.preferences"), {"message__site": "1", "message_whatsapp": "1"})

        self.assertEqual(response.status_code, 200)
        prefs = NotificationPreference.objects.get(profile=self.profile)
        self.assertTrue(prefs.message_whatsapp)

    def test_disconnecting_a_number_clears_a_previously_enabled_preference_on_next_save(self) -> None:
        self.profile.phone_number = "+15550001111"
        self.profile.save(update_fields=["phone_number"])
        self.client.post(reverse("notifications.preferences"), {"message__site": "1", "message_sms": "1"})
        self.assertTrue(NotificationPreference.objects.get(profile=self.profile).message_sms)

        self.profile.phone_number = ""
        self.profile.save(update_fields=["phone_number"])
        response = self.client.post(reverse("notifications.preferences"), {"message__site": "1", "message_sms": "1"})

        self.assertEqual(response.status_code, 200)
        self.assertFalse(NotificationPreference.objects.get(profile=self.profile).message_sms)

    def test_get_reports_connection_status_in_context(self) -> None:
        self.profile.whatsapp_number = ""
        self.profile.phone_number = "+15550001111"
        self.profile.save(update_fields=["whatsapp_number", "phone_number"])

        response = self.client.get(reverse("notifications.preferences"))

        self.assertFalse(response.context["has_whatsapp_number"])
        self.assertTrue(response.context["has_phone_number"])
