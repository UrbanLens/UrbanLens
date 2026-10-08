"""An opt-out made from an emailed link after a check-in is archived still stops future alerts to that person.

Archival scrubs a typed-in contact's address, so the link used to record its opt-out against a placeholder and the
person could be added, and alerted, again. A keyed hash of the address, kept on the archived contact, is what the
opt-out records instead - never the address itself, which the archive promises only the owner can read again.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
import datetime
import hashlib
import os
from typing import TYPE_CHECKING

from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.e2ee.key_bundle import MessagingKeyBundle
from urbanlens.dashboard.models.fields import reset_encryption_keys
from urbanlens.dashboard.models.safety.model import (
    SafetyCheckin,
    SafetyCheckinContact,
    SafetyCheckinStatus,
    SafetyContactOptOut,
    SafetyContactOptOutScope,
)
from urbanlens.dashboard.services.visits.safety import (
    archive_checkin,
    is_contact_opted_out,
    record_contact_opt_out,
    validate_notifiable_contacts,
)
from urbanlens.UrbanLens.settings.app import settings as app_settings

if TYPE_CHECKING:
    from collections.abc import Iterator

    from django.db.models import Model

    from urbanlens.dashboard.models.profile.model import Profile

_ADDRESS = "Res.Cuer+trips@googlemail.com"
_SAME_MAILBOX = "rescuer@gmail.com"
_KEY_A = "archived-contact-key-a-" + "x" * 40
_KEY_B = "archived-contact-key-b-" + "y" * 40


@contextmanager
def _using_keys(active: str, fallbacks: list[str] | None = None) -> Iterator[None]:
    original_active = app_settings.field_encryption_key
    original_fallbacks = app_settings.field_encryption_key_fallbacks
    app_settings.field_encryption_key = active
    app_settings.field_encryption_key_fallbacks = fallbacks or []
    reset_encryption_keys()
    try:
        yield
    finally:
        app_settings.field_encryption_key = original_active
        app_settings.field_encryption_key_fallbacks = original_fallbacks
        reset_encryption_keys()


def _verified(username: str, email: str) -> User:
    user = baker.make(User, username=username, email=email, is_active=True)
    user.profile.verified_primary_email = user.profile.primary_email_normalized
    user.profile.save(update_fields=["verified_primary_email"])
    return user


def _text_values(row: Model) -> list[str]:
    return [value for field in row._meta.concrete_fields if isinstance(value := getattr(row, field.attname), str)]


class _ArchivedContactTestCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.owner = baker.make(User, username="hiker").profile
        self.checkin = self._resolved_checkin(self.owner)
        now = timezone.now()
        self.contact = baker.make(
            SafetyCheckinContact, checkin=self.checkin, email=_ADDRESS, contact_profile=None, notified_at=now
        )
        self.quiet = baker.make(
            SafetyCheckinContact, checkin=self.checkin, email="quiet@example.com", contact_profile=None
        )
        MessagingKeyBundle.objects.create(
            profile=self.owner,
            public_key=base64.b64encode(os.urandom(32)).decode(),
            recovery_wrapped_secret=base64.b64encode(os.urandom(72)).decode(),
        )

    def _resolved_checkin(self, owner: Profile) -> SafetyCheckin:
        return baker.make(
            SafetyCheckin,
            profile=owner,
            title="Tunnel walk",
            checkin_by=timezone.now() - datetime.timedelta(hours=3),
            grace_period=datetime.timedelta(hours=1),
            status=SafetyCheckinStatus.CHECKED_IN,
            escalated_at=timezone.now() - datetime.timedelta(hours=1),
            resolved_at=timezone.now() - datetime.timedelta(hours=1),
            notify_community_wiki=False,
        )

    def _archive(self) -> None:
        archive_checkin(SafetyCheckin.objects.get(pk=self.checkin.pk))
        self.contact.refresh_from_db()
        self.quiet.refresh_from_db()

    def _opt_out_from_link(self, scope: SafetyContactOptOutScope):
        return self.client.post(reverse("safety.contact.optout", args=[self.contact.token, scope.value]), follow=True)

    def _blocked(self, *, owner: Profile, profile: Profile | None = None, email: str | None = None) -> bool:
        return is_contact_opted_out(profile, email, owner=owner)


class OptOutAfterArchivalTests(_ArchivedContactTestCase):
    def test_an_owner_scoped_opt_out_keeps_any_spelling_of_the_address_off_that_owner_s_next_checkin(self) -> None:
        self._archive()
        self._opt_out_from_link(SafetyContactOptOutScope.OWNER)

        allowed, rejected = validate_notifiable_contacts(self.owner, [(None, _SAME_MAILBOX, "")])

        self.assertEqual(allowed, [])
        self.assertEqual(len(rejected), 1)
        self.assertFalse(self._blocked(owner=baker.make(User).profile, email=_SAME_MAILBOX))

    def test_a_global_opt_out_holds_for_every_owner(self) -> None:
        self._archive()
        self._opt_out_from_link(SafetyContactOptOutScope.GLOBAL)

        self.assertTrue(self._blocked(owner=baker.make(User).profile, email=_SAME_MAILBOX))

    def test_it_holds_when_the_address_comes_back_as_the_account_that_verified_it(self) -> None:
        member = _verified("rescuer", _SAME_MAILBOX)
        self._archive()
        self._opt_out_from_link(SafetyContactOptOutScope.GLOBAL)

        self.assertTrue(self._blocked(owner=baker.make(User).profile, profile=member.profile))

    def test_the_link_says_it_worked(self) -> None:
        self._archive()

        response = self._opt_out_from_link(SafetyContactOptOutScope.GLOBAL)

        self.assertEqual(response.status_code, 200)
        self.assertIn("won't receive", " ".join(str(message) for message in get_messages(response.wsgi_request)))


class NoAddressIsKeptTests(_ArchivedContactTestCase):
    def test_neither_the_archived_contact_nor_its_opt_out_holds_the_address(self) -> None:
        self._archive()
        self._opt_out_from_link(SafetyContactOptOutScope.GLOBAL)
        (opt_out,) = SafetyContactOptOut.objects.all()

        forms = {_ADDRESS.lower(), _SAME_MAILBOX, "rescuer", "res.cuer"}
        bare_hashes = {hashlib.sha256(form.encode()).hexdigest() for form in forms}
        for row in (self.contact, opt_out):
            for value in _text_values(row):
                self.assertFalse(any(form in value.lower() for form in forms), f"{row!r} keeps {value!r}")
                self.assertNotIn(value, bare_hashes)
        self.assertIsNone(opt_out.email)

    def test_a_contact_never_alerted_keeps_no_hash_at_all(self) -> None:
        """Only an alerted contact was ever sent a link to opt out with."""
        self._archive()

        self.assertEqual(self.quiet.email_hmac, "")
        self.assertNotEqual(self.contact.email_hmac, "")

    def test_an_opt_out_for_that_checkin_alone_keeps_no_address_beside_the_archive(self) -> None:
        record_contact_opt_out(self.contact, SafetyContactOptOutScope.CHECKIN)

        self._archive()

        self.assertFalse(SafetyContactOptOut.objects.filter(email__isnull=False).exists())

    def test_the_hash_is_keyed_by_a_site_secret_and_survives_a_key_rotation(self) -> None:
        other_owner = baker.make(User).profile
        with _using_keys(_KEY_A):
            self._archive()
            self._opt_out_from_link(SafetyContactOptOutScope.GLOBAL)
            self.assertTrue(self._blocked(owner=other_owner, email=_SAME_MAILBOX))

        with _using_keys(_KEY_B):
            self.assertFalse(self._blocked(owner=other_owner, email=_SAME_MAILBOX))

        with _using_keys(_KEY_B, [_KEY_A]):
            self.assertTrue(self._blocked(owner=other_owner, email=_SAME_MAILBOX))


class CheckinArchivedBeforeTheHashTests(_ArchivedContactTestCase):
    """A check-in archived by earlier code kept no hash: its contact's address is simply gone."""

    def test_the_link_records_nothing_and_says_it_could_not(self) -> None:
        self._archive()
        SafetyCheckinContact.objects.filter(pk=self.contact.pk).update(email_hmac="")

        response = self._opt_out_from_link(SafetyContactOptOutScope.GLOBAL)

        self.assertEqual(SafetyContactOptOut.objects.count(), 0)
        text = " ".join(str(message) for message in get_messages(response.wsgi_request))
        self.assertNotIn("won't receive", text)
        self.assertIn("couldn't", text)
