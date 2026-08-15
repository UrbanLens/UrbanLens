"""The profile content fields round-trip through export and import.

Closes the gap recorded in PROBLEMS.md ("`profile` is exported but never
imported"): bio, area, dates and every contact handle sat visibly in the
user's own archive and were silently dropped on re-import. Identity
(username/email/date_joined) must stay untouched - an archive must not be
able to overwrite the login identity of the account it is imported into.
"""

from __future__ import annotations

import json
import tempfile

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.import_export.export import _export_profile
from urbanlens.dashboard.services.import_export.import_data import ImportResult, _import_profile


class ProfileRoundTripTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # bootstrap admin
        self.user = baker.make(User, username="keeper", email="keeper@example.test", first_name="Kay")
        self.profile = self.user.profile
        self.profile.bio = "urbex since 2009"
        self.profile.area = "Hudson Valley"
        self.profile.phone_number = "+15551234567"
        self.profile.signal_username = "kay.01"
        self.profile.save()

    def _round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            _export_profile(self.profile, temp_dir)
            # Blank everything the archive should restore.
            self.profile.bio = ""
            self.profile.area = ""
            self.profile.phone_number = ""
            self.profile.signal_username = ""
            self.profile.save()
            _import_profile(self.profile, temp_dir, ImportResult(), pin_uuid_map={}, label_uuid_map={})
        self.profile.refresh_from_db()
        self.user.refresh_from_db()

    def test_content_fields_are_restored(self) -> None:
        self._round_trip()
        self.assertEqual(self.profile.bio, "urbex since 2009")
        self.assertEqual(self.profile.area, "Hudson Valley")
        self.assertEqual(self.profile.phone_number, "+15551234567")
        self.assertEqual(self.profile.signal_username, "kay.01")
        self.assertEqual(self.user.first_name, "Kay")

    def test_identity_is_never_overwritten(self) -> None:
        """The archive names a username/email; importing must not apply them."""
        with tempfile.TemporaryDirectory() as temp_dir:
            _export_profile(self.profile, temp_dir)
            with open(f"{temp_dir}/profile.json", encoding="utf-8") as fh:
                data = json.load(fh)
            data["username"] = "impostor"
            data["email"] = "impostor@example.test"
            with open(f"{temp_dir}/profile.json", "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            _import_profile(self.profile, temp_dir, ImportResult(), pin_uuid_map={}, label_uuid_map={})
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, "keeper")
        self.assertEqual(self.user.email, "keeper@example.test")

    def test_a_pre_gap_archive_without_contact_block_blanks_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with open(f"{temp_dir}/profile.json", "w", encoding="utf-8") as fh:
                json.dump({"bio": "new bio"}, fh)
            _import_profile(self.profile, temp_dir, ImportResult(), pin_uuid_map={}, label_uuid_map={})
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.bio, "new bio")
        self.assertEqual(self.profile.phone_number, "+15551234567", "an archive without a contact block must not blank existing handles")
