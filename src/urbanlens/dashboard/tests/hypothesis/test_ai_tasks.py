"""Tests for services.ai.tasks.run_assistant_turn_task (batch 2c).

Calls the task directly (not via ``.delay()``/``.apply_async()``) - a bound
Celery task is a plain callable that runs synchronously and supplies
``self``, so this exercises the real function body, including its
``update_state`` calls, without needing a broker. Enqueue-time behavior
(``apply_async`` args, ``queue=``, ``expires=``) is covered generically by
``test_celery_helpers.py``'s ``SafelyEnqueueTaskTests``.
"""

from __future__ import annotations

from unittest.mock import patch

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.site_settings.model import SiteSettings
from urbanlens.dashboard.services.ai.assistant import AssistantTurn, AssistantUnavailableError
from urbanlens.dashboard.services.ai.tasks import _EXPIRED_REPLY, _UNAVAILABLE_REPLY, run_assistant_turn_task
from urbanlens.dashboard.services.ai.turns import acquire_turn_lock, turn_lock_is_current


def _plain_profile():
    """A profile with SiteFeature.AI granted, matching assistant_available()'s other gates by default."""
    from urbanlens.dashboard.models.subscriptions import SiteFeature

    baker.make("auth.User")  # bootstrap site admin absorption
    settings_obj = SiteSettings.get_current()
    SiteSettings.objects.filter(pk=settings_obj.pk).update(default_features=SiteFeature.AI)
    return Profile.objects.get(user=baker.make("auth.User"))


class RunAssistantTurnTaskTests(TestCase):
    def setUp(self) -> None:
        self.profile = _plain_profile()

    def test_successful_turn_returns_reply_and_releases_the_lock(self) -> None:
        token = acquire_turn_lock(self.profile)
        assert token is not None
        with patch("urbanlens.dashboard.services.ai.tasks.run_assistant_turn", return_value=AssistantTurn(reply="Hi!", actions=["Searched your pins"])):
            result = run_assistant_turn_task(self.profile.pk, [], "hello", token)
        self.assertEqual(result, {"reply": "Hi!", "actions": ["Searched your pins"]})
        # Released, not merely unexamined - a fresh acquire must now succeed.
        self.assertFalse(turn_lock_is_current(self.profile, token))
        self.assertIsNotNone(acquire_turn_lock(self.profile))

    def test_missing_profile_is_handled_without_raising(self) -> None:
        result = run_assistant_turn_task(999_999_999, [], "hello", "some-token")
        self.assertEqual(result, {"reply": _UNAVAILABLE_REPLY, "actions": []})

    def test_stale_lock_skips_execution_entirely(self) -> None:
        # This token was never actually granted the profile's current lock
        # (or the lock has since expired/been superseded) - the task must
        # not spend a provider call on a turn nothing is polling for.
        with patch("urbanlens.dashboard.services.ai.tasks.run_assistant_turn") as mock_run:
            result = run_assistant_turn_task(self.profile.pk, [], "hello", "not-the-real-token")
        mock_run.assert_not_called()
        self.assertEqual(result, {"reply": _EXPIRED_REPLY, "actions": []})

    def test_lock_superseded_by_a_newer_turn_is_left_alone(self) -> None:
        """A stale task holding an old token must never release a newer turn's lock."""
        stale_token = acquire_turn_lock(self.profile)
        assert stale_token is not None
        # Simulate the old lock expiring and a new turn claiming it.
        from urbanlens.dashboard.services.ai.turns import release_turn_lock

        release_turn_lock(self.profile, stale_token)
        fresh_token = acquire_turn_lock(self.profile)
        assert fresh_token is not None

        with patch("urbanlens.dashboard.services.ai.tasks.run_assistant_turn") as mock_run:
            result = run_assistant_turn_task(self.profile.pk, [], "hello", stale_token)

        mock_run.assert_not_called()
        self.assertEqual(result, {"reply": _EXPIRED_REPLY, "actions": []})
        # The newer turn's lock is untouched.
        self.assertTrue(turn_lock_is_current(self.profile, fresh_token))

    def test_assistant_unavailable_releases_the_lock(self) -> None:
        token = acquire_turn_lock(self.profile)
        assert token is not None
        with patch("urbanlens.dashboard.services.ai.tasks.assistant_available", return_value=False):
            result = run_assistant_turn_task(self.profile.pk, [], "hello", token)
        self.assertEqual(result, {"reply": _UNAVAILABLE_REPLY, "actions": []})
        self.assertFalse(turn_lock_is_current(self.profile, token))

    def test_assistant_unavailable_error_from_the_loop_releases_the_lock(self) -> None:
        token = acquire_turn_lock(self.profile)
        assert token is not None
        with patch("urbanlens.dashboard.services.ai.tasks.run_assistant_turn", side_effect=AssistantUnavailableError("off")):
            result = run_assistant_turn_task(self.profile.pk, [], "hello", token)
        self.assertEqual(result, {"reply": _UNAVAILABLE_REPLY, "actions": []})
        self.assertFalse(turn_lock_is_current(self.profile, token))

    def test_history_and_message_reach_run_assistant_turn(self) -> None:
        token = acquire_turn_lock(self.profile)
        assert token is not None
        history = [{"role": "user", "content": "earlier"}]
        with patch("urbanlens.dashboard.services.ai.tasks.run_assistant_turn", return_value=AssistantTurn(reply="ok")) as mock_run:
            run_assistant_turn_task(self.profile.pk, history, "hello", token)
        mock_run.assert_called_once_with(self.profile, history, "hello")
