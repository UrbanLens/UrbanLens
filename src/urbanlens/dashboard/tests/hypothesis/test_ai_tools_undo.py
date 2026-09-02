"""Tests for services.ai.tools.undo - undo_peek and undo_last_action, through registry.execute()."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import patch

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.baker_recipes import _make_profile
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.ai.tools.registry import ToolContext, execute
from urbanlens.dashboard.services.undo.service import UndoExpiredError, peek_undo, stash_for_undo

if TYPE_CHECKING:
    from urbanlens.dashboard.models.undo import UndoAction


def _plain_profile():
    """A profile with SiteFeature.AI granted - see test_ai_tools_registry.py's own docstring for why."""
    from urbanlens.dashboard.models.site_settings.model import SiteSettings
    from urbanlens.dashboard.models.subscriptions import SiteFeature

    baker.make("auth.User")
    settings_obj = SiteSettings.get_current()
    SiteSettings.objects.filter(pk=settings_obj.pk).update(default_features=SiteFeature.AI)
    return _make_profile()


def _context(profile) -> ToolContext:
    return ToolContext(profile=profile, now=datetime.now(tz=UTC))


def _stash_deleted_pin(profile, name: str = "Old Mill") -> UndoAction:
    """Delete a fresh pin for ``profile`` after stashing it, matching the real delete-then-stash order elsewhere."""
    pin = baker.make(Pin, profile=profile, name=name)
    undo_action = stash_for_undo("pin", [pin], profile)
    # stash_for_undo only returns None from inside an undo/redo apply - never
    # the case here, so this narrows for every call site below.
    assert undo_action is not None
    pin.delete()
    return undo_action


class UndoPeekToolTests(TestCase):
    def setUp(self) -> None:
        self.profile = _plain_profile()

    def test_nothing_to_undo(self) -> None:
        result = execute("undo_peek", {}, _context(self.profile))
        self.assertEqual(result.data, {"can_undo": False})

    def test_reports_the_top_entrys_label_and_uuid(self) -> None:
        undo_action = _stash_deleted_pin(self.profile)
        result = execute("undo_peek", {}, _context(self.profile))
        self.assertEqual(result.data, {"can_undo": True, "label": undo_action.object_repr, "undo_uuid": str(undo_action.uuid)})

    def test_another_profiles_undo_history_is_invisible(self) -> None:
        other = _plain_profile()
        _stash_deleted_pin(other)
        result = execute("undo_peek", {}, _context(self.profile))
        self.assertEqual(result.data, {"can_undo": False})


class UndoLastActionToolTests(TestCase):
    def setUp(self) -> None:
        self.profile = _plain_profile()

    def test_write_tool_produces_a_proposal_without_executing(self) -> None:
        undo_action = _stash_deleted_pin(self.profile)
        result = execute("undo_last_action", {"undo_uuid": str(undo_action.uuid)}, _context(self.profile), confirmed=False)
        self.assertIsNotNone(result.proposal)
        assert result.proposal is not None
        self.assertEqual(result.proposal["args"], {"undo_uuid": str(undo_action.uuid)})
        undo_action.refresh_from_db()
        self.assertIsNone(undo_action.undone_at)

    def test_confirmed_with_the_current_uuid_restores_it(self) -> None:
        undo_action = _stash_deleted_pin(self.profile)
        result = execute("undo_last_action", {"undo_uuid": str(undo_action.uuid)}, _context(self.profile))
        self.assertNotIn("error", result.data)
        self.assertEqual(result.data["status"], "undone")
        undo_action.refresh_from_db()
        self.assertIsNotNone(undo_action.undone_at)
        self.assertTrue(Pin.objects.filter(profile=self.profile, name="Old Mill").exists())

    def test_a_stale_uuid_is_refused_and_nothing_is_restored(self) -> None:
        """The user did something else after undo_peek - a later undo must not be confirmed against the earlier uuid."""
        first = _stash_deleted_pin(self.profile, name="First")
        second = _stash_deleted_pin(self.profile, name="Second")  # now the top of the stack instead of `first`

        result = execute("undo_last_action", {"undo_uuid": str(first.uuid)}, _context(self.profile))

        self.assertIn("error", result.data)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertIsNone(first.undone_at)
        self.assertIsNone(second.undone_at)

    def test_nothing_to_undo_is_an_error_not_a_raise(self) -> None:
        result = execute("undo_last_action", {"undo_uuid": "00000000-0000-0000-0000-000000000000"}, _context(self.profile))
        self.assertIn("error", result.data)

    def test_another_profiles_real_uuid_cannot_be_used_against_this_profiles_stack(self) -> None:
        other = _plain_profile()
        theirs = _stash_deleted_pin(other, name="Theirs")
        mine = _stash_deleted_pin(self.profile, name="Mine")

        result = execute("undo_last_action", {"undo_uuid": str(theirs.uuid)}, _context(self.profile))

        self.assertIn("error", result.data)
        theirs.refresh_from_db()
        mine.refresh_from_db()
        self.assertIsNone(theirs.undone_at)
        self.assertIsNone(mine.undone_at)

    def test_missing_args_is_an_error_block_not_a_raise(self) -> None:
        result = execute("undo_last_action", {}, _context(self.profile))
        self.assertIn("error", result.data)

    def test_an_expired_restore_surfaces_as_an_error_not_a_raise(self) -> None:
        """UndoAlreadyRestoredError (a concurrent double-confirm) - covered by mocking since peek_undo's own .active() filter already screens out a genuinely time-expired row before restore_undo_action ever runs."""
        undo_action = _stash_deleted_pin(self.profile)
        # _undo_last_action imports restore_undo_action locally at call time
        # (matching every other handler's deferred-import convention), so the
        # patch target is where it's defined, not the tool module's own name.
        with patch("urbanlens.dashboard.services.undo.service.restore_undo_action", side_effect=UndoExpiredError("already restored")):
            result = execute("undo_last_action", {"undo_uuid": str(undo_action.uuid)}, _context(self.profile))
        self.assertIn("error", result.data)

    def test_write_tool_is_summarized_not_a_read_summary(self) -> None:
        undo_action = _stash_deleted_pin(self.profile)
        result = execute("undo_last_action", {"undo_uuid": str(undo_action.uuid)}, _context(self.profile))
        self.assertEqual(result.summary, "Undid it")


class UndoToolsIntegrationTests(TestCase):
    """undo_peek's own uuid, fed straight into undo_last_action, is the whole point of the pair."""

    def setUp(self) -> None:
        self.profile = _plain_profile()

    def test_peek_then_confirm_round_trip(self) -> None:
        _stash_deleted_pin(self.profile)
        peek_result = execute("undo_peek", {}, _context(self.profile))
        self.assertTrue(peek_result.data["can_undo"])

        confirm_result = execute("undo_last_action", {"undo_uuid": peek_result.data["undo_uuid"]}, _context(self.profile))

        self.assertNotIn("error", confirm_result.data)
        self.assertIsNone(peek_undo(self.profile))
