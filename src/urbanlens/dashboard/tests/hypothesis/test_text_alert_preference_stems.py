"""WhatsApp/SMS toggles must actually reach the code that reads them.

Two independent defects met here, and either alone was enough to make a toggle
a lie:

1. `_enabled_channels` derived the preference column from the
   notification's *type value*. That works for 31 of 32 types, but
   `SAFETY_CHECKIN_PARTNER_INVITE` has the value `safety_ci_partner_invite`
   while its columns are `safety_checkin_partner_invite*`. `getattr`'s `False`
   default then reported "user does not want text alerts" - indistinguishable
   from a real opt-out. It now resolves by enum *member name*, which every other
   consumer of these preferences already uses.
2. `TEXT_ALERTABLE_TYPES` omitted that type entirely, so the lookup was never
   even reached. Its own docstring defines membership as "types that have a
   toggle pair", with MESSAGE as the single deliberate exclusion - and the
   partner invite has a full pair, persisted and settable through the external
   API. The omission was an oversight, not a decision.

Together those meant a user could enable WhatsApp/SMS for safety check-in
partner invites and never receive one, with nothing logged anywhere.
"""

from __future__ import annotations

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.notifications.meta.type import NotificationType
from urbanlens.dashboard.models.notifications.model import NotificationLog, NotificationPreference
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.notifications.notification_text_alerts import (
    TEXT_ALERTABLE_TYPES,
    _enabled_channels,
)

#: The one type deliberately excluded - DMs keep their own alert pipeline.
_DELIBERATELY_EXCLUDED = {"message"}


def _stems_with_a_toggle_pair() -> set[str]:
    columns = {field.name for field in NotificationPreference._meta.get_fields()}
    return {
        notification_type.value
        for notification_type in NotificationType
        if f"{notification_type.name.lower()}_whatsapp" in columns and f"{notification_type.name.lower()}_sms" in columns
    }


class AlertableTypeCoverageTests(TestCase):
    def test_every_type_with_a_toggle_pair_is_alertable(self) -> None:
        """The rule the set's own docstring states, enforced.

        A settable toggle that no code path can honour is worse than no toggle.
        """
        unreachable = _stems_with_a_toggle_pair() - set(TEXT_ALERTABLE_TYPES) - _DELIBERATELY_EXCLUDED

        self.assertEqual(unreachable, set(), "these preference toggles are settable but can never fire")

    def test_no_alertable_type_lacks_a_toggle_pair(self) -> None:
        """The converse: nothing in the set can read a column that doesn't exist."""
        self.assertEqual(set(TEXT_ALERTABLE_TYPES) - _stems_with_a_toggle_pair(), set())


class PreferenceLookupTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = Profile.objects.get(user=baker.make("auth.User"))
        self.prefs = NotificationPreference.objects.get_or_create(profile=self.profile)[0]

    def _notification(self, notification_type: str) -> NotificationLog:
        return NotificationLog.objects.create(
            profile=self.profile,
            notification_type=notification_type,
            title="t",
            message="m",
        )

    def test_partner_invite_toggles_are_read_despite_the_value_name_mismatch(self) -> None:
        """The type whose value and column stem disagree."""
        self.prefs.safety_checkin_partner_invite_whatsapp = True
        self.prefs.safety_checkin_partner_invite_sms = True
        self.prefs.save(update_fields=["safety_checkin_partner_invite_whatsapp", "safety_checkin_partner_invite_sms"])

        whatsapp, sms = _enabled_channels(self._notification(NotificationType.SAFETY_CHECKIN_PARTNER_INVITE))

        self.assertTrue(whatsapp)
        self.assertTrue(sms)

    def test_an_untouched_preference_still_reads_false(self) -> None:
        """The fix must not turn a missing column into an accidental opt-in."""
        whatsapp, sms = _enabled_channels(self._notification(NotificationType.SAFETY_CHECKIN_PARTNER_INVITE))

        self.assertFalse(whatsapp)
        self.assertFalse(sms)

    def test_a_type_whose_value_already_matched_still_works(self) -> None:
        """Resolving by member name must not regress the other 31 types."""
        self.prefs.friend_accepted_whatsapp = True
        self.prefs.save(update_fields=["friend_accepted_whatsapp"])

        whatsapp, sms = _enabled_channels(self._notification(NotificationType.FRIEND_ACCEPTED))

        self.assertTrue(whatsapp)
        self.assertFalse(sms)
