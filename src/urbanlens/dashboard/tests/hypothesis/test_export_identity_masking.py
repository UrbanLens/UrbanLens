"""A data export must not disclose identities the app masks on screen.

The direct-message export states this rule in its own docstring: it passes each
partner through ``display_identity_for`` "so an export never reveals a partner's
name/avatar beyond what the user could currently see on screen (e.g. after being
blocked or a privacy change)".

The trips export writes ``p.user.username`` for every member straight into
``trips.json``, while the trip page itself resolves those same members through
``resolve_visible_identities`` and masks the ones the viewer may not see. So the
export hands over names the page withholds.
"""

from __future__ import annotations

import json
import os
import tempfile

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.models.trips.model import Trip
from urbanlens.dashboard.services.import_export.export import _export_direct_messages, _export_trips


def _profile(visibility: str = VisibilityChoice.ANYONE) -> Profile:
    profile = baker.make("auth.User").profile
    Profile.objects.filter(pk=profile.pk).update(profile_visibility=visibility, direct_message_visibility=VisibilityChoice.ANYONE)
    profile.refresh_from_db()
    profile.ensure_slug()
    return profile


class TripExportIdentityMaskingTests(TestCase):
    """Trip members the viewer may not identify stay unidentified in the export."""

    def setUp(self):
        super().setUp()
        self.exporter = _profile()
        # NO_ONE: this member's identity is masked to everyone but themselves.
        self.private_member = _profile(VisibilityChoice.NO_ONE)
        self.open_member = _profile()

        self.trip = Trip.objects.create(name="Shared trip", creator=self.exporter)
        self.trip.profiles.add(self.exporter, self.private_member, self.open_member)

    def _export(self) -> list[dict]:
        with tempfile.TemporaryDirectory() as temp_dir:
            _export_trips(self.exporter, temp_dir)
            with open(os.path.join(temp_dir, "trips.json"), encoding="utf-8") as fh:
                return json.load(fh)

    def test_the_trip_page_masks_this_member(self):
        # Establishes the premise: the export is inconsistent with the screen,
        # not with a rule nobody applies.
        self.assertFalse(self.private_member.can_view_profile(self.exporter))
        self.assertTrue(self.open_member.can_view_profile(self.exporter))

    def test_a_masked_members_username_is_not_in_the_export(self):
        rows = self._export()

        self.assertEqual(len(rows), 1)
        self.assertNotIn(self.private_member.username, rows[0]["members"])

    def test_a_visible_members_username_is_still_exported(self):
        rows = self._export()

        self.assertIn(self.open_member.username, rows[0]["members"])

    def test_the_exporters_own_username_is_still_exported(self):
        rows = self._export()

        self.assertIn(self.exporter.username, rows[0]["members"])

    def test_the_member_list_keeps_one_entry_per_member(self):
        # Masking must not silently drop people - the trip still has three
        # members and the count is not itself a secret.
        rows = self._export()

        self.assertEqual(len(rows[0]["members"]), 3)

    def test_the_creator_field_is_masked_too(self):
        other_trip = Trip.objects.create(name="Someone else's trip", creator=self.private_member)
        other_trip.profiles.add(self.exporter, self.private_member)

        rows = self._export()
        row = next(r for r in rows if r["name"] == "Someone else's trip")

        self.assertNotEqual(row["creator"], self.private_member.username)


class DirectMessageExportMaskingTests(TestCase):
    """The DM export already applies this rule - a guard so it stays that way."""

    def test_a_masked_partners_username_is_not_in_the_export(self):
        from urbanlens.dashboard.models.direct_messages.model import DirectMessage

        exporter = _profile()
        partner = _profile(VisibilityChoice.NO_ONE)
        DirectMessage.objects.create(sender=partner, recipient=exporter, body="hello")

        with tempfile.TemporaryDirectory() as temp_dir:
            _export_direct_messages(exporter, temp_dir)
            with open(os.path.join(temp_dir, "direct_messages.json"), encoding="utf-8") as fh:
                payload = fh.read()

        self.assertNotIn(partner.username, payload)
