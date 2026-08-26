"""Tests for migration 0048's data pass over the newly-encrypted free-text fields.

``AlterField`` never rewrites stored bytes, so retrofitting ``EncryptedTextField``
onto a populated column leaves plaintext behind that raises ``InvalidToken`` on
its first ORM read (``fail_soft`` fields degrade to empty instead, which is worse
- the value looks deleted rather than broken). ``encrypt_existing_preference_fields``
is the companion pass that closes that gap.

Rows are seeded with raw SQL to reproduce the pre-migration state, exactly as
``test_encrypted_tokens_migration`` does for the 0007/0018 token pass. The
migration module's name starts with a digit, so ``importlib`` is used rather than
a normal import.
"""

from __future__ import annotations

from importlib import import_module

from django.db import connection
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.friendship.invitation.model import FriendInvitation
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.safety.model import EmergencyContactDefault

_migration = import_module("urbanlens.dashboard.migrations.0030_v0_7_0")
encrypt_existing_preference_fields = _migration.encrypt_existing_preference_fields


class _FakeSchemaEditor:
    """Minimal stand-in exposing the one attribute the data migration needs."""

    connection = connection


class EncryptPreferenceFieldsMigrationTests(TestCase):
    """The data pass re-encrypts pre-existing plaintext in place, and only once."""

    def test_profile_preference_text_survives_the_retrofit(self) -> None:
        profile: Profile = baker.make(Profile)
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE dashboard_profiles SET additional_preferences = %s, photo_taking_preference_other = %s WHERE id = %s",
                ["please ask before posting me", "only with a hood up", profile.pk],
            )

        encrypt_existing_preference_fields(apps=None, schema_editor=_FakeSchemaEditor())

        refreshed = Profile.objects.get(pk=profile.pk)
        self.assertEqual(refreshed.additional_preferences, "please ask before posting me")
        self.assertEqual(refreshed.photo_taking_preference_other, "only with a hood up")

    def test_contact_label_survives_the_retrofit(self) -> None:
        # email (not contact_profile) to satisfy the exactly-one-target check
        # constraint; it is itself encrypted, which is what the label now joins.
        contact: EmergencyContactDefault = baker.make(EmergencyContactDefault, email="dana@example.com", contact_profile=None, label="")
        with connection.cursor() as cursor:
            cursor.execute("UPDATE dashboard_safety_contact_defaults SET label = %s WHERE id = %s", ["Dana (sister)", contact.pk])

        encrypt_existing_preference_fields(apps=None, schema_editor=_FakeSchemaEditor())

        self.assertEqual(EmergencyContactDefault.objects.get(pk=contact.pk).label, "Dana (sister)")

    def test_invitation_note_survives_the_retrofit(self) -> None:
        invitation: FriendInvitation = baker.make(FriendInvitation, email="newcomer@example.com", message=None)
        with connection.cursor() as cursor:
            cursor.execute("UPDATE dashboard_friendinvitation SET message = %s WHERE id = %s", ["come explore with us", invitation.pk])

        encrypt_existing_preference_fields(apps=None, schema_editor=_FakeSchemaEditor())

        self.assertEqual(FriendInvitation.objects.get(pk=invitation.pk).message, "come explore with us")

    def test_invitation_email_is_left_plaintext(self) -> None:
        """The signup path matches open invitations by exact email - encrypting it would silently break that."""
        baker.make(FriendInvitation, email="newcomer@example.com", message=None)

        encrypt_existing_preference_fields(apps=None, schema_editor=_FakeSchemaEditor())

        self.assertTrue(FriendInvitation.objects.filter(email="newcomer@example.com").exists())

    def test_the_column_really_holds_ciphertext_afterwards(self) -> None:
        """Guards against the pass silently no-op'ing and leaving plaintext at rest."""
        profile: Profile = baker.make(Profile)
        with connection.cursor() as cursor:
            cursor.execute("UPDATE dashboard_profiles SET additional_preferences = %s WHERE id = %s", ["ask first", profile.pk])

        encrypt_existing_preference_fields(apps=None, schema_editor=_FakeSchemaEditor())

        with connection.cursor() as cursor:
            cursor.execute("SELECT additional_preferences FROM dashboard_profiles WHERE id = %s", [profile.pk])
            stored = cursor.fetchone()[0]
        self.assertNotEqual(stored, "ask first")
        self.assertTrue(stored.startswith("gAAAAA"), stored[:20])

    def test_running_twice_does_not_double_encrypt(self) -> None:
        """A re-run (retried deploy, squashed-migration replay) must be harmless."""
        profile: Profile = baker.make(Profile)
        with connection.cursor() as cursor:
            cursor.execute("UPDATE dashboard_profiles SET additional_preferences = %s WHERE id = %s", ["ask first", profile.pk])

        encrypt_existing_preference_fields(apps=None, schema_editor=_FakeSchemaEditor())
        encrypt_existing_preference_fields(apps=None, schema_editor=_FakeSchemaEditor())

        # Double-encrypting would make the ORM read return the inner ciphertext
        # rather than the plaintext, so this assertion is the real check.
        self.assertEqual(Profile.objects.get(pk=profile.pk).additional_preferences, "ask first")

    def test_empty_values_are_left_alone(self) -> None:
        profile: Profile = baker.make(Profile)

        encrypt_existing_preference_fields(apps=None, schema_editor=_FakeSchemaEditor())

        with connection.cursor() as cursor:
            cursor.execute("SELECT additional_preferences FROM dashboard_profiles WHERE id = %s", [profile.pk])
            self.assertEqual(cursor.fetchone()[0], "")
