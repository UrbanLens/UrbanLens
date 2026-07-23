"""Tests for the generic per-type WhatsApp/SMS notification alerts.

Every ``NotificationPreference`` ``<type>_whatsapp``/``<type>_sms`` toggle
must actually deliver (docs/PROBLEMS.md; decision 2026-07-23) - these cover
the central scheduling signal, the delayed re-checking task, and the
per-(recipient, type) debounce. The DM pipeline keeps its own tests in
``test_direct_messages.py``.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.notifications.meta import Importance, NotificationType, Status
from urbanlens.dashboard.models.notifications.model import NotificationLog, NotificationPreference
from urbanlens.dashboard.services.notification_text_alerts import (
    TEXT_ALERTABLE_TYPES,
    schedule_notification_text_alerts,
    send_notification_text_alerts_now,
)
from urbanlens.dashboard.tasks import send_notification_text_alerts_if_unread

_ENQUEUE_PATCH = "urbanlens.dashboard.services.celery.safely_enqueue_task"
_WHATSAPP_PATCH = "urbanlens.dashboard.services.notification_delivery.send_whatsapp"
_SMS_PATCH = "urbanlens.dashboard.services.notification_delivery.send_sms"


class _AlertTestBase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile

    def _prefs(self, **kwargs) -> NotificationPreference:
        prefs, _ = NotificationPreference.objects.get_or_create(profile=self.profile)
        for field, value in kwargs.items():
            setattr(prefs, field, value)
        prefs.save()
        return prefs

    def _notification(self, ntype: str = NotificationType.PIN_SHARED, **kwargs) -> NotificationLog:
        defaults = {
            "profile": self.profile,
            "status": Status.UNREAD,
            "importance": Importance.MEDIUM,
            "notification_type": ntype,
            "title": "Pin shared with you",
            "message": "A friend shared Old Mill with you.",
        }
        defaults.update(kwargs)
        return NotificationLog.objects.create(**defaults)


class ScheduleNotificationTextAlertsTests(_AlertTestBase):
    """schedule_notification_text_alerts: cheap gating before any task is queued."""

    def test_no_preference_row_enqueues_nothing(self) -> None:
        notification = self._notification()
        with mock.patch(_ENQUEUE_PATCH) as enqueue:
            schedule_notification_text_alerts(notification)
        enqueue.assert_not_called()

    def test_toggles_off_enqueues_nothing(self) -> None:
        self._prefs()
        notification = self._notification()
        with mock.patch(_ENQUEUE_PATCH) as enqueue:
            schedule_notification_text_alerts(notification)
        enqueue.assert_not_called()

    def test_whatsapp_toggle_on_enqueues_delayed_task(self) -> None:
        self._prefs(pin_shared_whatsapp=True)
        notification = self._notification()
        with mock.patch(_ENQUEUE_PATCH) as enqueue:
            schedule_notification_text_alerts(notification)
        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.args[1], notification.pk)
        self.assertGreater(enqueue.call_args.kwargs.get("countdown", 0), 0)

    def test_sms_toggle_alone_is_sufficient(self) -> None:
        self._prefs(trip_updated_sms=True)
        notification = self._notification(ntype=NotificationType.TRIP_UPDATED)
        with mock.patch(_ENQUEUE_PATCH) as enqueue:
            schedule_notification_text_alerts(notification)
        enqueue.assert_called_once()

    def test_message_type_is_excluded_from_the_generic_pipeline(self) -> None:
        """DM alerts keep their own streak-debounced pipeline - the generic one
        must not double-text for MESSAGE notifications."""
        self._prefs(message_whatsapp=True)
        notification = self._notification(ntype=NotificationType.MESSAGE)
        with mock.patch(_ENQUEUE_PATCH) as enqueue:
            schedule_notification_text_alerts(notification)
        enqueue.assert_not_called()

    def test_untoggled_type_is_excluded(self) -> None:
        notification = self._notification(ntype=NotificationType.INFO)
        self._prefs(pin_shared_whatsapp=True)
        with mock.patch(_ENQUEUE_PATCH) as enqueue:
            schedule_notification_text_alerts(notification)
        enqueue.assert_not_called()

    def test_every_alertable_type_has_both_preference_fields(self) -> None:
        prefs = self._prefs()
        for ntype in TEXT_ALERTABLE_TYPES:
            self.assertTrue(hasattr(prefs, f"{ntype}_whatsapp"), ntype)
            self.assertTrue(hasattr(prefs, f"{ntype}_sms"), ntype)

    def test_signal_schedules_on_creation(self) -> None:
        """The post_save signal wires creation to scheduling after commit.

        The native-push signal shares safely_enqueue_task, so assert on the
        specific task rather than the total call count.
        """
        self._prefs(pin_shared_whatsapp=True)
        with mock.patch(_ENQUEUE_PATCH) as enqueue, self.captureOnCommitCallbacks(execute=True):
            notification = self._notification()
        text_alert_calls = [c for c in enqueue.call_args_list if c.args and c.args[0] is send_notification_text_alerts_if_unread]
        self.assertEqual(len(text_alert_calls), 1)
        self.assertEqual(text_alert_calls[0].args[1], notification.pk)


class SendNotificationTextAlertsTaskTests(_AlertTestBase):
    """The delayed task re-checks read state, sends per channel, and debounces."""

    def test_read_notification_is_never_texted(self) -> None:
        self._prefs(pin_shared_whatsapp=True)
        notification = self._notification(status=Status.READ)
        with mock.patch(_WHATSAPP_PATCH) as whatsapp, mock.patch(_SMS_PATCH) as sms:
            send_notification_text_alerts_if_unread(notification.pk)
        whatsapp.assert_not_called()
        sms.assert_not_called()

    def test_unread_notification_sends_on_enabled_channels_only(self) -> None:
        self._prefs(pin_shared_whatsapp=True, pin_shared_sms=False)
        notification = self._notification()
        with mock.patch(_WHATSAPP_PATCH) as whatsapp, mock.patch(_SMS_PATCH) as sms:
            send_notification_text_alerts_if_unread(notification.pk)
        whatsapp.assert_called_once()
        sms.assert_not_called()

    def test_body_carries_the_title_not_the_message(self) -> None:
        """Details stay off the third-party carrier - only the title travels."""
        self._prefs(pin_shared_whatsapp=True)
        notification = self._notification()
        with mock.patch(_WHATSAPP_PATCH) as whatsapp:
            send_notification_text_alerts_if_unread(notification.pk)
        body = whatsapp.call_args.args[1]
        self.assertIn("Pin shared with you", body)
        self.assertNotIn("Old Mill", body)

    def test_second_same_type_alert_is_debounced(self) -> None:
        self._prefs(pin_shared_whatsapp=True)
        first = self._notification()
        second = self._notification()
        with mock.patch(_WHATSAPP_PATCH) as whatsapp:
            send_notification_text_alerts_if_unread(first.pk)
            send_notification_text_alerts_if_unread(second.pk)
        whatsapp.assert_called_once()

    def test_different_types_debounce_independently(self) -> None:
        self._prefs(pin_shared_whatsapp=True, trip_updated_whatsapp=True)
        pin_shared = self._notification()
        trip_updated = self._notification(ntype=NotificationType.TRIP_UPDATED, title="Trip updated")
        with mock.patch(_WHATSAPP_PATCH) as whatsapp:
            send_notification_text_alerts_if_unread(pin_shared.pk)
            send_notification_text_alerts_if_unread(trip_updated.pk)
        self.assertEqual(whatsapp.call_count, 2)

    def test_toggles_turned_off_after_enqueue_are_respected(self) -> None:
        self._prefs()
        notification = self._notification()
        with mock.patch(_WHATSAPP_PATCH) as whatsapp, mock.patch(_SMS_PATCH) as sms:
            send_notification_text_alerts_now(notification)
        whatsapp.assert_not_called()
        sms.assert_not_called()

    def test_deleted_notification_is_a_quiet_no_op(self) -> None:
        with mock.patch(_WHATSAPP_PATCH) as whatsapp:
            send_notification_text_alerts_if_unread(999_999)
        whatsapp.assert_not_called()
