"""Tests for self-service account deletion.

Covers:
- ProfileQuerySet.due_for_deletion_reminder / due_for_hard_delete boundary conditions.
- services.profile.account_deletion: request/cancel/reminder/hard-delete, notifications, emails, idempotency.
- RequestAccountDeletionView / CancelAccountDeletionView controllers.
- The site-wide deletion banner rendering.
"""

from __future__ import annotations

import datetime
import smtplib
from unittest import mock

from django.contrib.auth.models import Permission, User
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone
from hypothesis import given, settings as hyp_settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.labels.meta import KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.notifications.meta import NotificationType
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import (
    ACCOUNT_DELETION_GRACE_PERIOD,
    ACCOUNT_DELETION_REMINDER_LEAD,
    Profile,
)
from urbanlens.dashboard.models.trips.model import Trip, TripComment
from urbanlens.dashboard.services.profile.account_deletion import (
    cancel_deletion,
    hard_delete_profile,
    request_deletion,
    send_deletion_reminder,
)


def _fake_image(name: str) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, b"fake-image-bytes", content_type="image/png")


def _backdate_request(profile: Profile, ago: datetime.timedelta) -> Profile:
    Profile.objects.filter(pk=profile.pk).update(
        deletion_requested_at=timezone.now() - ago, deletion_reminder_sent_at=None
    )
    profile.refresh_from_db()
    return profile


def _new_profile() -> Profile:
    """A Profile has a required OneToOne to User, auto-created by a post_save signal on User -
    baker.make(Profile) directly would race that signal and violate the unique constraint.
    """
    return baker.make(User).profile


class ProfileQuerySetDueForDeletionReminderTests(TestCase):
    """due_for_deletion_reminder() selects profiles ~1 day from their hard delete, not yet reminded."""

    def test_excludes_profile_with_no_pending_deletion(self):
        profile = _new_profile()
        self.assertNotIn(profile, Profile.objects.due_for_deletion_reminder())

    def test_excludes_profile_freshly_requested(self):
        profile = _backdate_request(_new_profile(), datetime.timedelta(hours=1))
        self.assertNotIn(profile, Profile.objects.due_for_deletion_reminder())

    def test_includes_profile_exactly_at_reminder_threshold(self):
        profile = _backdate_request(_new_profile(), ACCOUNT_DELETION_GRACE_PERIOD - datetime.timedelta(days=1))
        self.assertIn(profile, Profile.objects.due_for_deletion_reminder())

    def test_includes_profile_past_reminder_threshold(self):
        profile = _backdate_request(_new_profile(), ACCOUNT_DELETION_GRACE_PERIOD)
        self.assertIn(profile, Profile.objects.due_for_deletion_reminder())

    def test_excludes_profile_already_reminded(self):
        profile = _backdate_request(_new_profile(), ACCOUNT_DELETION_GRACE_PERIOD)
        Profile.objects.filter(pk=profile.pk).update(deletion_reminder_sent_at=timezone.now())
        profile.refresh_from_db()
        self.assertNotIn(profile, Profile.objects.due_for_deletion_reminder())

    @given(hours_ago=st.floats(min_value=0, max_value=143, allow_nan=False, allow_infinity=False))
    @hyp_settings(deadline=None)
    def test_never_includes_profiles_under_six_days_old(self, hours_ago: float):
        profile = _backdate_request(_new_profile(), datetime.timedelta(hours=hours_ago))
        self.assertNotIn(profile, Profile.objects.due_for_deletion_reminder())


class ProfileQuerySetDueForHardDeleteTests(TestCase):
    """due_for_hard_delete() selects profiles whose 7-day grace period has fully elapsed."""

    def test_excludes_profile_with_no_pending_deletion(self):
        profile = _new_profile()
        self.assertNotIn(profile, Profile.objects.due_for_hard_delete())

    def test_excludes_profile_within_grace_period(self):
        profile = _backdate_request(_new_profile(), ACCOUNT_DELETION_GRACE_PERIOD - datetime.timedelta(hours=1))
        self.assertNotIn(profile, Profile.objects.due_for_hard_delete())

    def test_includes_profile_exactly_at_grace_period(self):
        profile = _backdate_request(_new_profile(), ACCOUNT_DELETION_GRACE_PERIOD)
        self.assertIn(profile, Profile.objects.due_for_hard_delete())

    def test_includes_profile_past_grace_period(self):
        profile = _backdate_request(_new_profile(), ACCOUNT_DELETION_GRACE_PERIOD + datetime.timedelta(days=1))
        self.assertIn(profile, Profile.objects.due_for_hard_delete())

    @given(days_ago=st.floats(min_value=0, max_value=6.99, allow_nan=False, allow_infinity=False))
    @hyp_settings(deadline=None)
    def test_never_includes_profiles_under_seven_days_old(self, days_ago: float):
        profile = _backdate_request(_new_profile(), datetime.timedelta(days=days_ago))
        self.assertNotIn(profile, Profile.objects.due_for_hard_delete())


class RequestDeletionTests(TestCase):
    """request_deletion() soft-deletes, notifies on-site, and emails the owner."""

    def setUp(self):
        self.user = baker.make(User, email="owner@example.com")
        self.profile = self.user.profile

    def test_sets_deletion_requested_at(self):
        request_deletion(self.profile)
        self.assertIsNotNone(self.profile.deletion_requested_at)

    def test_marks_profile_as_pending_deletion(self):
        request_deletion(self.profile)
        self.assertTrue(self.profile.is_pending_deletion)

    def test_creates_onsite_notification(self):
        request_deletion(self.profile)
        self.assertTrue(
            NotificationLog.objects.filter(
                profile=self.profile, notification_type=NotificationType.ACCOUNT_DELETION_REQUESTED
            ).exists()
        )

    def test_sends_email(self):
        request_deletion(self.profile)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["owner@example.com"])

    def test_clears_any_stale_reminder_flag(self):
        Profile.objects.filter(pk=self.profile.pk).update(deletion_reminder_sent_at=timezone.now())
        self.profile.refresh_from_db()
        request_deletion(self.profile)
        self.assertIsNone(self.profile.deletion_reminder_sent_at)

    def test_no_email_sent_when_profile_has_no_email(self):
        self.profile.user.email = ""
        self.profile.user.save(update_fields=["email"])
        # Assert test assumptions
        self.assertEqual(len(mail.outbox), 0, "Outbox is 0 before test run")
        self.assertFalse(
            self.profile.is_pending_deletion, "User started with pending deletion, possible unsanitary test db"
        )
        self.assertFalse(
            NotificationLog.objects.filter(
                profile=self.profile, notification_type=NotificationType.ACCOUNT_DELETION_REQUESTED
            ).exists(),
            "Possible unsanitary test db",
        )

        request_deletion(self.profile)
        self.assertEqual(len(mail.outbox), 0)
        self.assertTrue(self.profile.is_pending_deletion)
        self.assertTrue(
            NotificationLog.objects.filter(
                profile=self.profile, notification_type=NotificationType.ACCOUNT_DELETION_REQUESTED
            ).exists()
        )

    def test_deletion_still_recorded_when_email_send_fails(self):
        """`_send_email` logs and swallows delivery failures - a raised SMTPException must not abort the request."""
        self.assertFalse(
            self.profile.is_pending_deletion, "User started with pending deletion, possible unsanitary test db"
        )
        self.assertFalse(
            NotificationLog.objects.filter(
                profile=self.profile, notification_type=NotificationType.ACCOUNT_DELETION_REQUESTED
            ).exists(),
            "Possible unsanitary test db",
        )

        with mock.patch("django.core.mail.EmailMultiAlternatives.send", side_effect=smtplib.SMTPException("nope")):
            request_deletion(self.profile)
        self.assertTrue(self.profile.is_pending_deletion)
        self.assertTrue(
            NotificationLog.objects.filter(
                profile=self.profile, notification_type=NotificationType.ACCOUNT_DELETION_REQUESTED
            ).exists()
        )


class CancelDeletionTests(TestCase):
    """cancel_deletion() clears the pending state entirely."""

    def test_clears_deletion_requested_at(self):
        profile = _backdate_request(_new_profile(), datetime.timedelta(days=1))
        cancel_deletion(profile)
        self.assertFalse(profile.is_pending_deletion)

    def test_clears_reminder_sent_at(self):
        profile = _backdate_request(_new_profile(), datetime.timedelta(days=1))
        Profile.objects.filter(pk=profile.pk).update(deletion_reminder_sent_at=timezone.now())
        profile.refresh_from_db()
        cancel_deletion(profile)
        self.assertIsNone(profile.deletion_reminder_sent_at)


class SendDeletionReminderTests(TestCase):
    """send_deletion_reminder() is idempotent and notifies both channels."""

    def setUp(self):
        self.user = baker.make(User, email="owner@example.com")
        self.profile = _backdate_request(self.user.profile, ACCOUNT_DELETION_GRACE_PERIOD)

    def test_creates_onsite_notification(self):
        send_deletion_reminder(self.profile)
        self.assertTrue(
            NotificationLog.objects.filter(
                profile=self.profile, notification_type=NotificationType.ACCOUNT_DELETION_REMINDER
            ).exists()
        )

    def test_sends_email(self):
        send_deletion_reminder(self.profile)
        self.assertEqual(len(mail.outbox), 1)

    def test_stamps_reminder_sent_at(self):
        send_deletion_reminder(self.profile)
        self.assertIsNotNone(self.profile.deletion_reminder_sent_at)

    def test_profile_no_longer_due_after_reminder(self):
        send_deletion_reminder(self.profile)
        self.assertNotIn(self.profile, Profile.objects.due_for_deletion_reminder())

    def test_no_email_sent_when_profile_has_no_email(self):
        self.profile.user.email = ""
        self.profile.user.save(update_fields=["email"])
        self.assertEqual(len(mail.outbox), 0, "Test assumptions fail")
        self.assertIsNone(self.profile.deletion_reminder_sent_at, "Test assumptions fail")

        send_deletion_reminder(self.profile)
        self.assertEqual(len(mail.outbox), 0)
        self.assertIsNotNone(self.profile.deletion_reminder_sent_at)

    def test_reminder_still_stamped_when_email_send_fails(self):
        """A raised SMTPException must not stop the idempotency marker from being set,
        or the sweep would resend this reminder forever."""
        self.assertIsNone(self.profile.deletion_reminder_sent_at, "Test assumptions fail")
        with mock.patch("django.core.mail.EmailMultiAlternatives.send", side_effect=smtplib.SMTPException("nope")):
            send_deletion_reminder(self.profile)
        self.assertIsNotNone(self.profile.deletion_reminder_sent_at)


class HardDeleteProfileTests(TestCase):
    """hard_delete_profile() emails, then permanently removes the account and its data."""

    def setUp(self):
        self.user = baker.make(User, email="owner@example.com", username="doomed")
        self.profile = _backdate_request(self.user.profile, ACCOUNT_DELETION_GRACE_PERIOD)
        self.pin = baker.make(Pin, profile=self.profile)

    def test_sends_final_email(self):
        hard_delete_profile(self.profile)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["owner@example.com"])

    def test_deletes_the_user_row(self):
        user_pk = self.user.pk
        hard_delete_profile(self.profile)
        self.assertFalse(User.objects.filter(pk=user_pk).exists())

    def test_deletes_the_profile_row(self):
        profile_pk = self.profile.pk
        hard_delete_profile(self.profile)
        self.assertFalse(Profile.objects.filter(pk=profile_pk).exists())

    def test_cascades_to_owned_pins(self):
        pin_pk = self.pin.pk
        hard_delete_profile(self.profile)
        self.assertFalse(Pin.objects.filter(pk=pin_pk).exists())

    def test_no_final_email_when_user_has_no_email(self):
        self.profile.user.email = ""
        self.profile.user.save(update_fields=["email"])
        user_pk = self.user.pk
        self.assertEqual(len(mail.outbox), 0, "Test assumptions fail")
        self.assertTrue(User.objects.filter(pk=user_pk).exists(), "Test assumptions fail")
        hard_delete_profile(self.profile)
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(User.objects.filter(pk=user_pk).exists())

    def test_account_still_deleted_when_final_email_fails(self):
        """A raised SMTPException while sending the "account deleted" email must not
        abort the hard delete - a broken mail relay must never leave the account undeleted."""
        user_pk = self.user.pk
        self.assertTrue(User.objects.filter(pk=user_pk).exists(), "Test assumptions fail")
        with mock.patch("django.core.mail.EmailMultiAlternatives.send", side_effect=smtplib.SMTPException("nope")):
            hard_delete_profile(self.profile)
        self.assertFalse(User.objects.filter(pk=user_pk).exists())


class HardDeleteProfileFileCleanupTests(TestCase):
    """hard_delete_profile() must not orphan storage files for rows whose DB
    row is about to cascade-delete - Django never deletes a FileField's file
    on row deletion, so account_deletion.py has to do it explicitly."""

    def setUp(self):
        self.user = baker.make(User, email="owner@example.com", username="doomed")
        self.profile = _backdate_request(self.user.profile, ACCOUNT_DELETION_GRACE_PERIOD)

    def test_avatar_file_is_deleted(self):
        self.profile.avatar = _fake_image("avatar.png")
        self.profile.save(update_fields=["avatar"])
        storage, name = self.profile.avatar.storage, self.profile.avatar.name
        hard_delete_profile(self.profile)
        self.assertFalse(storage.exists(name))

    def test_uploaded_image_file_is_deleted(self):
        from urbanlens.dashboard.models.images.model import Image

        image = Image.objects.create(image=_fake_image("photo.jpg"), profile=self.profile)
        storage, name = image.image.storage, image.image.name
        hard_delete_profile(self.profile)
        self.assertFalse(storage.exists(name))

    def test_pin_custom_icon_file_is_deleted(self):
        pin = baker.make(Pin, profile=self.profile, custom_icon=_fake_image("icon.png"))
        storage, name = pin.custom_icon.storage, pin.custom_icon.name
        hard_delete_profile(self.profile)
        self.assertFalse(storage.exists(name))

    def test_comment_image_file_is_deleted(self):
        pin = baker.make(Pin)  # comment on someone else's pin - only the author's file must be cleaned up
        comment = baker.make(Comment, pin=pin, wiki=None, profile=self.profile, image=_fake_image("comment.png"))
        storage, name = comment.image.storage, comment.image.name
        hard_delete_profile(self.profile)
        self.assertFalse(storage.exists(name))

    def test_label_custom_icon_file_is_deleted(self):
        label = baker.make(Label, kind=KIND_TAG, profile=self.profile, custom_icon=_fake_image("label.png"))
        storage, name = label.custom_icon.storage, label.custom_icon.name
        hard_delete_profile(self.profile)
        self.assertFalse(storage.exists(name))

    def test_trip_comment_survives_with_its_image_intact(self):
        """TripComment.author is SET_NULL by design - the row and its image
        outlive account deletion so other trip members keep the thread; only
        the author reference is cleared."""
        other = baker.make(User)
        trip = baker.make(Trip, creator=other.profile)
        comment = baker.make(TripComment, trip=trip, author=self.profile, image=_fake_image("trip.png"))
        storage, name = comment.image.storage, comment.image.name
        hard_delete_profile(self.profile)
        comment.refresh_from_db()
        self.assertIsNone(comment.author_id)
        self.assertTrue(storage.exists(name))

    def test_missing_file_on_disk_does_not_block_deletion(self):
        """A DB row pointing at an already-missing file must not crash the sweep."""
        pin = baker.make(Pin, profile=self.profile, custom_icon=_fake_image("icon.png"))
        pin.custom_icon.storage.delete(pin.custom_icon.name)
        hard_delete_profile(self.profile)  # must not raise
        self.assertFalse(User.objects.filter(pk=self.user.pk).exists())


class RequestAccountDeletionViewTests(TestCase):
    """POST /settings/delete-account/ requires a correct password and typed confirmation."""

    def setUp(self):
        baker.make(User)  # occupies the "first user" bootstrap slot so alice isn't auto-promoted to site admin
        self.user = baker.make(User, username="alice", email="alice@example.com")
        self.user.set_password("correct-horse")
        self.user.save()
        self.client.force_login(self.user)

    def test_wrong_password_does_not_schedule_deletion(self):
        self.client.post(reverse("account.delete.request"), {"password": "wrong", "confirm_text": "delete alice"})
        self.user.profile.refresh_from_db()
        self.assertFalse(self.user.profile.is_pending_deletion)

    def test_wrong_confirm_text_does_not_schedule_deletion(self):
        self.client.post(
            reverse("account.delete.request"), {"password": "correct-horse", "confirm_text": "delete somebody-else"}
        )
        self.user.profile.refresh_from_db()
        self.assertFalse(self.user.profile.is_pending_deletion)

    def test_correct_password_and_confirmation_schedules_deletion(self):
        self.client.post(
            reverse("account.delete.request"), {"password": "correct-horse", "confirm_text": "delete alice"}
        )
        self.user.profile.refresh_from_db()
        self.assertTrue(self.user.profile.is_pending_deletion)

    def test_user_stays_logged_in_after_request(self):
        response = self.client.post(
            reverse("account.delete.request"),
            {"password": "correct-horse", "confirm_text": "delete alice"},
            follow=True,
        )
        self.assertTrue(response.wsgi_request.user.is_authenticated)

    def test_confirm_text_is_case_insensitive(self):
        self.client.post(
            reverse("account.delete.request"), {"password": "correct-horse", "confirm_text": "DELETE ALICE"}
        )
        self.user.profile.refresh_from_db()
        self.assertTrue(self.user.profile.is_pending_deletion)

    def test_superuser_cannot_schedule_deletion(self):
        self.user.is_superuser = True
        self.user.save()
        self.client.post(
            reverse("account.delete.request"), {"password": "correct-horse", "confirm_text": "delete alice"}
        )
        self.user.profile.refresh_from_db()
        self.assertFalse(self.user.profile.is_pending_deletion)

    def test_view_site_admin_permission_blocks_deletion(self):
        """The gate is `is_superuser OR has_perm(view_site_admin)` - a staff moderator
        holding just the permission (not superuser) must be blocked too."""
        self.user.user_permissions.add(Permission.objects.get(codename="view_site_admin"))
        self.client.post(
            reverse("account.delete.request"), {"password": "correct-horse", "confirm_text": "delete alice"}
        )
        self.user.profile.refresh_from_db()
        self.assertFalse(self.user.profile.is_pending_deletion)


class CancelAccountDeletionViewTests(TestCase):
    """POST /settings/delete-account/cancel/ undoes a pending deletion for the logged-in user."""

    def setUp(self):
        self.user = baker.make(User)
        self.profile = _backdate_request(self.user.profile, datetime.timedelta(days=1))
        self.client.force_login(self.user)

    def test_cancels_pending_deletion(self):
        self.client.post(reverse("account.delete.cancel"))
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.is_pending_deletion)

    def test_redirect_only_honors_a_same_site_next(self):
        """`next` is an attacker-controlled POST field, not just the hidden banner
        input - an off-site value must fall back to the settings page rather than
        becoming an open redirect."""
        response = self.client.post(reverse("account.delete.cancel"), {"next": "https://evil.example.com/phish"})
        self.assertEqual(response.url, reverse("settings.view"))

        response = self.client.post(reverse("account.delete.cancel"), {"next": "/map/"})
        self.assertEqual(response.url, "/map/")


class AccountDeletionBannerTests(TestCase):
    """The site-wide banner renders only while the logged-in user's deletion is pending."""

    def setUp(self):
        self.user = baker.make(User)
        self.client.force_login(self.user)

    def test_banner_absent_when_not_pending(self):
        response = self.client.get(reverse("settings.view"))
        self.assertNotContains(response, "account-deletion-banner")

    def test_banner_present_when_pending(self):
        _backdate_request(self.user.profile, datetime.timedelta(days=1))
        response = self.client.get(reverse("settings.view"))
        self.assertContains(response, "account-deletion-banner")


class DeletionReminderOverlapLockTests(TestCase):
    """Two overlapping sweeps must not both email the same account.

    `due_for_deletion_reminder` filters on `deletion_reminder_sent_at__isnull=True`,
    which guards at *selection* time, and `send_deletion_reminder` emails first
    and stamps the marker afterwards - so without the overlap lock two runs both
    select the same profile and both send. Celery delivers at least once, and a
    slow sweep can outlast its own beat interval, so "two runs at once" is
    ordinary rather than exotic.

    The three sibling reminder sweeps (`send_due_checkin_reminders`,
    `send_final_checkin_warnings`, `escalate_overdue_checkins`) already take this
    lock; this one did not.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User, email="leaving@example.com")
        self.profile = self.user.profile
        self.profile.deletion_requested_at = timezone.now() - (
            ACCOUNT_DELETION_GRACE_PERIOD - ACCOUNT_DELETION_REMINDER_LEAD
        )
        self.profile.save(update_fields=["deletion_requested_at"])

    def test_a_run_holding_the_lock_blocks_a_concurrent_one(self) -> None:
        from urbanlens.dashboard.services.core.locks import acquire_lock, release_lock
        from urbanlens.dashboard.tasks import (
            _DELETION_REMINDER_LOCK_CACHE_KEY,
            _DELETION_REMINDER_LOCK_TIMEOUT_SECONDS,
            send_account_deletion_reminders,
        )

        token = acquire_lock(_DELETION_REMINDER_LOCK_CACHE_KEY, _DELETION_REMINDER_LOCK_TIMEOUT_SECONDS)
        self.addCleanup(release_lock, _DELETION_REMINDER_LOCK_CACHE_KEY, token)
        self.assertIsNotNone(token, "precondition: the lock must be free before the test takes it")

        sent = send_account_deletion_reminders()

        self.assertEqual(sent, 0, "the sweep ran while another held the lock")
        self.profile.refresh_from_db()
        self.assertIsNone(self.profile.deletion_reminder_sent_at, "a blocked run must leave the profile still due")

    def test_the_blocked_profile_is_still_sent_on_the_next_tick(self) -> None:
        """The lock must not lose a reminder - only defer it.

        This is why a lock is right here and a claim-before-send is not: a
        duplicate notice is noise, a missing one means no warning at all before
        a permanent deletion.
        """
        from urbanlens.dashboard.services.core.locks import acquire_lock, release_lock
        from urbanlens.dashboard.tasks import (
            _DELETION_REMINDER_LOCK_CACHE_KEY,
            _DELETION_REMINDER_LOCK_TIMEOUT_SECONDS,
            send_account_deletion_reminders,
        )

        token = acquire_lock(_DELETION_REMINDER_LOCK_CACHE_KEY, _DELETION_REMINDER_LOCK_TIMEOUT_SECONDS)
        send_account_deletion_reminders()
        release_lock(_DELETION_REMINDER_LOCK_CACHE_KEY, token)

        self.assertEqual(send_account_deletion_reminders(), 1)
        self.profile.refresh_from_db()
        self.assertIsNotNone(self.profile.deletion_reminder_sent_at)

    def test_an_uncontended_sweep_still_sends(self) -> None:
        """Anti-vacuity: the lock must not stop the ordinary path."""
        from urbanlens.dashboard.tasks import send_account_deletion_reminders

        self.assertEqual(send_account_deletion_reminders(), 1)
        self.profile.refresh_from_db()
        self.assertIsNotNone(self.profile.deletion_reminder_sent_at)

    def test_a_second_sweep_sends_nothing_further(self) -> None:
        from urbanlens.dashboard.tasks import send_account_deletion_reminders

        send_account_deletion_reminders()
        self.assertEqual(send_account_deletion_reminders(), 0)
