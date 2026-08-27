"""A blocked profile can still be a saved emergency contact - and you are told.

Filed 2026-08-17: `EmergencyContactDefault` is a template copied onto each new
check-in, so blocking someone does not stop a check-in created afterwards from
paging them. The filing left it open because both silent answers are wrong in
an obvious way - leaving it pages someone you blocked, deleting it destroys a
safety contact in the one feature whose purpose is that somebody is told when
you do not come back.

So neither is chosen for the owner: the row stays, and the check-in and
settings pages say plainly that it will still be contacted. Someone may block
a person socially and still want them called if they go missing; that is
theirs to decide, knowingly.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.safety import EmergencyContactDefault
from urbanlens.dashboard.services.social.friendship import block_profile
from urbanlens.dashboard.services.visits.safety import blocked_default_contacts


class BlockedDefaultContactTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile: Profile = self.user.profile
        self.contact: Profile = baker.make(User).profile
        self.client.force_login(self.user)

    def _save_default(self, contact: Profile) -> EmergencyContactDefault:
        return EmergencyContactDefault.objects.create(owner=self.profile, contact_profile=contact, label="Partner")

    def test_an_unblocked_contact_raises_nothing(self) -> None:
        self._save_default(self.contact)

        self.assertEqual(blocked_default_contacts(self.profile), [])

    def test_a_blocked_contact_is_reported(self) -> None:
        self._save_default(self.contact)

        block_profile(self.profile, self.contact)

        self.assertEqual([p.pk for p in blocked_default_contacts(self.profile)], [self.contact.pk])

    def test_blocking_does_not_delete_the_contact(self) -> None:
        """The whole point: it is reported, not silently destroyed."""
        default = self._save_default(self.contact)

        block_profile(self.profile, self.contact)

        self.assertTrue(EmergencyContactDefault.objects.filter(pk=default.pk).exists())

    def test_it_holds_whichever_side_placed_the_block(self) -> None:
        """Blocking is an absolute veto in both directions (Profile.are_blocked)."""
        self._save_default(self.contact)

        block_profile(self.contact, self.profile)

        self.assertEqual([p.pk for p in blocked_default_contacts(self.profile)], [self.contact.pk])

    def test_an_email_only_default_has_no_profile_to_check(self) -> None:
        EmergencyContactDefault.objects.create(owner=self.profile, email="someone@example.test", label="Friend")

        self.assertEqual(blocked_default_contacts(self.profile), [])

    def test_the_check_in_form_says_so(self) -> None:
        self._save_default(self.contact)
        block_profile(self.profile, self.contact)

        response = self.client.get(reverse("safety.checkin.create"))

        self.assertContains(response, "blocked", msg_prefix="the warning must appear where the check-in is created")

    def test_a_clean_check_in_form_stays_quiet(self) -> None:
        self._save_default(self.contact)

        response = self.client.get(reverse("safety.checkin.create"))

        self.assertNotContains(response, "safety-blocked-contact-warning")
