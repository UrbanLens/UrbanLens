"""Privacy properties of the user data export (services/export.py).

The export had no tests at all; these lock in the two properties that
matter most:

- The connections exporter must not reveal the identity (or the response)
  behind outgoing requests the recipient hasn't accepted - the same
  account-enumeration rule the pending-requests widget enforces. Before
  this fix, inviting an email and exporting your data revealed whether the
  email matched a registered account (a Friendship row with the target's
  username/uuid appears only when it matched) and whether they declined.
- Exporters only ever emit the exporting user's own rows.
"""

from __future__ import annotations

import json
import os

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.export import _export_connections, _export_pins


def _read(temp_dir: str, filename: str):
    with open(os.path.join(temp_dir, filename), encoding="utf-8") as fh:
        return json.load(fh)


class ExportConnectionsPrivacyTests(TestCase):
    """Outgoing not-yet-accepted rows are anonymized; everything else keeps identity."""

    def setUp(self) -> None:
        super().setUp()
        self.exporter = baker.make(User, username="exporter").profile
        self.target = baker.make(User, username="hiddentarget").profile

    def _export(self) -> list[dict]:
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            _export_connections(self.exporter, temp_dir)
            return _read(temp_dir, "connections.json")

    def _outgoing(self, status: str) -> list[dict]:
        Friendship.objects.all().delete()
        Friendship.objects.create(from_profile=self.exporter, to_profile=self.target, status=status)
        return self._export()

    def test_outgoing_pending_request_is_anonymized(self) -> None:
        rows = self._outgoing(FriendshipStatus.REQUESTED)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["other_username"])
        self.assertIsNone(rows[0]["other_user_uuid"])
        self.assertEqual(rows[0]["status"], "pending")

    def test_declined_and_ignored_are_indistinguishable_from_pending(self) -> None:
        """The sender must not learn from their export whether (or how) the
        recipient responded - all three states export identically."""
        pending = self._outgoing(FriendshipStatus.REQUESTED)
        declined = self._outgoing(FriendshipStatus.DECLINED)
        ignored = self._outgoing(FriendshipStatus.IGNORED)
        for rows in (pending, declined, ignored):
            self.assertEqual(rows[0]["status"], "pending")
            self.assertIsNone(rows[0]["other_username"])
        # Byte-identical modulo the row's own timestamp.
        strip = lambda row: {k: v for k, v in row.items() if k != "created"}  # noqa: E731
        self.assertEqual(strip(pending[0]), strip(declined[0]))
        self.assertEqual(strip(pending[0]), strip(ignored[0]))

    def test_accepted_friendship_keeps_identity_in_both_directions(self) -> None:
        rows = self._outgoing(FriendshipStatus.ACCEPTED)
        self.assertEqual(rows[0]["other_username"], "hiddentarget")
        self.assertEqual(rows[0]["status"], FriendshipStatus.ACCEPTED)

        Friendship.objects.all().delete()
        Friendship.objects.create(from_profile=self.target, to_profile=self.exporter, status=FriendshipStatus.ACCEPTED)
        incoming_rows = self._export()
        self.assertEqual(incoming_rows[0]["other_username"], "hiddentarget")
        self.assertEqual(incoming_rows[0]["direction"], "incoming")

    def test_incoming_pending_request_keeps_identity(self) -> None:
        """The RECIPIENT legitimately sees who requested them - same as the
        incoming-requests widget."""
        Friendship.objects.create(from_profile=self.target, to_profile=self.exporter, status=FriendshipStatus.REQUESTED)
        rows = self._export()
        self.assertEqual(rows[0]["other_username"], "hiddentarget")
        self.assertEqual(rows[0]["status"], FriendshipStatus.REQUESTED)

    def test_sender_initiated_block_keeps_identity(self) -> None:
        """Blocking requires already knowing who you're blocking - nothing to hide."""
        rows = self._outgoing(FriendshipStatus.BLOCKED)
        self.assertEqual(rows[0]["other_username"], "hiddentarget")
        self.assertEqual(rows[0]["status"], FriendshipStatus.BLOCKED)


class ExportScopingTests(TestCase):
    """Exporters emit only the exporting user's own rows."""

    def test_pins_export_excludes_other_users_pins(self) -> None:
        import tempfile

        exporter = baker.make(User).profile
        other = baker.make(User).profile
        own_pin = baker.make(Pin, profile=exporter, name="Mine")
        baker.make(Pin, profile=other, name="Not Mine")

        with tempfile.TemporaryDirectory() as temp_dir:
            _export_pins(exporter, temp_dir)
            rows = _read(temp_dir, "pins.json")

        self.assertEqual([row["uuid"] for row in rows], [str(own_pin.uuid)])
        self.assertNotIn("Not Mine", json.dumps(rows))
