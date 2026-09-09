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
from urbanlens.dashboard.models.notifications.meta import DeliveryPreference
from urbanlens.dashboard.models.notifications.model import NotificationPreference


class NotificationChannelGatingTests(TestCase):
    def setUp(self) -> None:
        self.user: User = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_whatsapp_toggle_is_rejected_without_a_whatsapp_number(self) -> None:
        self.profile.whatsapp_number = ""
        self.profile.save(update_fields=["whatsapp_number"])

        response = self.client.post(
            reverse("notifications.preferences"), {"message__site": "1", "message_whatsapp": "1"}
        )

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

        response = self.client.post(
            reverse("notifications.preferences"), {"message__site": "1", "message_whatsapp": "1"}
        )

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


class EmailChannelHonestyTests(TestCase):
    """Settings must not offer "Email" for a category with no email-sending code.

    Before this fix, choosing "Email" over "Notification" for eight categories
    (friend_request, friend_accepted, added_to_trip, comment_reply,
    comment_liked, pin_shared, visit_suggested, achievement_earned) silently
    behaved exactly like "Notification" - the in-app row still fired, but no
    email was ever sent, and the settings page never said so.
    """

    def setUp(self) -> None:
        self.user: User = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_email_column_is_marked_unavailable_for_an_unimplemented_category(self) -> None:
        response = self.client.get(reverse("notifications.preferences"))

        body = response.content.decode()
        # friend_request's Email cell carries the unavailable class/tooltip;
        # its Notification/None cells (and the row generally) must not.
        self.assertIn("notif-prefs__cell--unavailable", body)
        self.assertIn("Email delivery isn't available for this yet", body)

    def test_email_column_is_not_marked_unavailable_for_a_working_category(self) -> None:
        response = self.client.get(reverse("notifications.preferences"))

        # "message" is the one category with a real email-sending path
        # (services.messaging.direct_messages) - its email checkbox must stay
        # a normal, fully interactive one, not the unavailable/locked cell.
        body = response.content.decode()
        idx = body.index('name="message__email"')
        cell_start = body.rindex("<label", 0, idx)
        cell_html = body[cell_start:idx]
        self.assertNotIn("unavailable", cell_html)
        self.assertIn('onchange="notifDelivery(this)"', body[idx : idx + 300])

    def test_an_existing_email_preference_survives_an_unrelated_toggle(self) -> None:
        """The unavailable cell must stay non-disabled, or an unrelated save
        in the same auto-submitting form would silently drop it to NONE/SITE."""
        prefs = NotificationPreference.objects.get_or_create(profile=self.profile)[0]
        prefs.friend_request = DeliveryPreference.EMAIL
        prefs.save(update_fields=["friend_request"])

        # Toggling a completely different field's Notification checkbox
        # re-submits the whole auto-saving form - friend_request's own
        # checkboxes travel along in that same POST.
        response = self.client.post(
            reverse("notifications.preferences"),
            {"friend_request__email": "1", "achievement_earned__site": "1"},
        )

        self.assertEqual(response.status_code, 200)
        prefs.refresh_from_db()
        self.assertEqual(prefs.friend_request, DeliveryPreference.EMAIL)
