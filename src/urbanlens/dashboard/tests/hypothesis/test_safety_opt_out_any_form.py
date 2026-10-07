"""A contact's opt-out holds however an owner adds them: as an account, or by any spelling of an address that account verified."""

from __future__ import annotations

import datetime

from django.contrib.auth.models import User
from django.core import mail
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.celery_inline import notification_emails_sent
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.notifications.meta import NotificationType
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.profile.email import ProfileEmail
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.safety.model import (
    SafetyCheckin,
    SafetyCheckinContact,
    SafetyCheckinStatus,
    SafetyContactOptOut,
    SafetyContactOptOutScope,
)
from urbanlens.dashboard.services.visits.safety import (
    escalate_checkin,
    mark_found_safe,
    notify_contacts_of_update,
    record_contact_opt_out,
    set_checkin_contacts,
    validate_notifiable_contacts,
)


def _verified(username: str, email: str) -> User:
    user = baker.make(User, username=username, email=email, is_active=True)
    user.profile.verified_primary_email = user.profile.primary_email_normalized
    user.profile.save(update_fields=["verified_primary_email"])
    return user


class _OptOutTestCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.owner = baker.make(User, username="hiker", email="hiker@example.com").profile
        self.member = _verified("member", "member@example.com")
        self.checkin = self._checkin(self.owner)

    def _checkin(self, owner: Profile, **extra) -> SafetyCheckin:
        fields = {
            "title": "Tunnel walk",
            "checkin_by": timezone.now() - datetime.timedelta(hours=2),
            "grace_period": datetime.timedelta(hours=1),
            "status": SafetyCheckinStatus.AWAITING_CHECKIN,
            "notify_community_wiki": False,
            **extra,
        }
        return baker.make(SafetyCheckin, profile=owner, **fields)

    def _opt_out_by_email(
        self,
        address: str,
        scope: SafetyContactOptOutScope = SafetyContactOptOutScope.GLOBAL,
        *,
        owner: Profile | None = None,
    ) -> None:
        """Opt out the way an emailed contact does: through the link on an earlier check-in that named ``address``."""
        earlier = self._checkin(owner or baker.make(User).profile, status=SafetyCheckinStatus.OVERDUE)
        record_contact_opt_out(
            baker.make(SafetyCheckinContact, checkin=earlier, email=address, contact_profile=None), scope
        )

    def _emailed(self) -> set[str]:
        return {recipient.lower() for message in mail.outbox for recipient in message.to}

    def _alerted_in_app(
        self, profile: Profile, notification_type: str = NotificationType.SAFETY_CHECKIN_OVERDUE
    ) -> bool:
        return NotificationLog.objects.filter(profile=profile, notification_type=notification_type).exists()


class ProfileContactWhoOptedOutByEmailTests(_OptOutTestCase):
    """The account is the contact; the opt-out was recorded against one of its addresses."""

    def setUp(self) -> None:
        super().setUp()
        set_checkin_contacts(self.checkin, [(self.member.profile, None, "")])

    def test_is_neither_emailed_nor_alerted_in_app(self) -> None:
        """The opt-out page promises they won't be "notified", not just that the email stops."""
        self._opt_out_by_email("member@example.com")

        escalate_checkin(self.checkin)

        self.assertNotIn("member@example.com", self._emailed())
        self.assertFalse(self._alerted_in_app(self.member.profile))

    def test_any_spelling_of_the_address_counts(self) -> None:
        gmail_member = _verified("gmail_member", "jdoe@gmail.com")
        set_checkin_contacts(self.checkin, [(gmail_member.profile, None, "")])
        self._opt_out_by_email("J.Doe+Safety@GoogleMail.com")

        escalate_checkin(self.checkin)

        self.assertNotIn("jdoe@gmail.com", self._emailed())
        self.assertFalse(self._alerted_in_app(gmail_member.profile))

    def test_a_verified_secondary_address_counts(self) -> None:
        ProfileEmail.objects.create(profile=self.member.profile, email="backup@example.com", is_verified=True)
        self._opt_out_by_email("backup@example.com")

        escalate_checkin(self.checkin)

        self.assertNotIn("member@example.com", self._emailed())
        self.assertFalse(self._alerted_in_app(self.member.profile))

    def test_an_owner_scoped_opt_out_holds_for_that_owner_only(self) -> None:
        self._opt_out_by_email("member@example.com", SafetyContactOptOutScope.OWNER, owner=self.owner)
        elsewhere = self._checkin(baker.make(User).profile)
        set_checkin_contacts(elsewhere, [(self.member.profile, None, "")])

        escalate_checkin(self.checkin)
        self.assertFalse(self._alerted_in_app(self.member.profile))

        escalate_checkin(elsewhere)
        self.assertTrue(self._alerted_in_app(self.member.profile))
        self.assertIn("member@example.com", self._emailed())

    def test_an_unverified_address_stops_the_email_but_not_the_account_alert(self) -> None:
        """Nothing proves the account is whoever opted that mailbox out, so the account is still alerted; the mailbox is not mailed."""
        unproven = baker.make(User, username="unproven", email="unproven@example.com", is_active=True)
        set_checkin_contacts(self.checkin, [(unproven.profile, None, "")])
        self._opt_out_by_email("unproven@example.com")

        escalate_checkin(self.checkin)

        self.assertNotIn("unproven@example.com", self._emailed())
        self.assertTrue(self._alerted_in_app(unproven.profile))

    def test_an_unrelated_address_does_not_block_them(self) -> None:
        self._opt_out_by_email("someone.else@example.com")

        escalate_checkin(self.checkin)

        self.assertIn("member@example.com", self._emailed())
        self.assertTrue(self._alerted_in_app(self.member.profile))

    def test_adding_them_again_is_refused(self) -> None:
        self._opt_out_by_email("member@example.com", SafetyContactOptOutScope.OWNER, owner=self.owner)

        allowed, rejected = validate_notifiable_contacts(self.owner, [(self.member.profile, None, "")])

        self.assertEqual(allowed, [])
        self.assertEqual(len(rejected), 1)

    def test_a_plan_update_after_escalation_skips_them(self) -> None:
        escalate_checkin(self.checkin)
        mail.outbox.clear()
        NotificationLog.objects.all().delete()
        self._opt_out_by_email("member@example.com")
        self.checkin.refresh_from_db()

        with notification_emails_sent():
            notify_contacts_of_update(self.checkin, "updated their trip plan")

        self.assertNotIn("member@example.com", self._emailed())
        self.assertFalse(self._alerted_in_app(self.member.profile, NotificationType.SAFETY_CHECKIN_PLAN_UPDATED))

    def test_the_found_safe_notice_skips_them(self) -> None:
        set_checkin_contacts(self.checkin, [(self.member.profile, None, ""), (None, "finder@example.com", "")])
        finder = self.checkin.contacts.get(email="finder@example.com")
        # Alerted, so only the opt-out keeps the notice from them.
        self.checkin.contacts.update(notified_at=timezone.now())
        self._opt_out_by_email("member@example.com")

        with notification_emails_sent():
            mark_found_safe(finder)

        self.assertNotIn("member@example.com", self._emailed())
        self.assertFalse(self._alerted_in_app(self.member.profile, NotificationType.SAFETY_CHECKIN_RESOLVED))


class EmailContactOptOutSpellingTests(_OptOutTestCase):
    """The contact was added by address; the opt-out was recorded under another spelling or another verified address."""

    def test_a_different_spelling_of_the_same_mailbox_is_honoured(self) -> None:
        set_checkin_contacts(self.checkin, [(None, "j.doe@gmail.com", "")])
        self._opt_out_by_email("JDoe+trips@googlemail.com")

        escalate_checkin(self.checkin)

        self.assertNotIn("j.doe@gmail.com", self._emailed())

    def test_an_opt_out_on_another_address_the_account_verified_is_honoured(self) -> None:
        ProfileEmail.objects.create(profile=self.member.profile, email="backup@example.com", is_verified=True)
        set_checkin_contacts(self.checkin, [(None, "backup@example.com", "")])
        self._opt_out_by_email("member@example.com")

        escalate_checkin(self.checkin)

        self.assertNotIn("backup@example.com", self._emailed())
        self.assertFalse(self._alerted_in_app(self.member.profile))

    def test_an_unverified_holder_of_another_address_is_not_tied_to_it(self) -> None:
        """An address on an account that never proved it stays its own identity."""
        squatter = baker.make(User, username="squatter", email="squatted@example.com", is_active=True)
        ProfileEmail.objects.create(profile=squatter.profile, email="claimed@example.com", is_verified=False)
        set_checkin_contacts(self.checkin, [(None, "squatted@example.com", "")])
        self._opt_out_by_email("claimed@example.com")

        escalate_checkin(self.checkin)

        self.assertIn("squatted@example.com", self._emailed())

    def test_a_row_written_before_the_normalized_column_still_matches_as_typed(self) -> None:
        """Code from before 0071, still serving during a rolling deploy, writes no normalized copy."""
        set_checkin_contacts(self.checkin, [(None, "J.Doe@gmail.com", "")])
        self._opt_out_by_email("J.Doe@gmail.com")
        SafetyContactOptOut.objects.update(email_normalized="")

        escalate_checkin(self.checkin)

        self.assertNotIn("j.doe@gmail.com", self._emailed())
