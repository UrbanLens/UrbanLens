"""Tests for services.spotguessr.chat - session-scoped live text chat (UL-392)."""

from __future__ import annotations

from unittest.mock import patch

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.spotguessr.model import GameSession, SpotGuessrMode
from urbanlens.dashboard.services.spotguessr.chat import CHAT_HISTORY_LIMIT, MAX_MESSAGE_LENGTH, recent_messages, send_chat_message


def _make_profile() -> Profile:
    return Profile.objects.get(user=baker.make("auth.User"))


def _make_session(host: Profile) -> GameSession:
    return baker.make(GameSession, host_profile=host, mode=SpotGuessrMode.PHOTOS)


class SendChatMessageTests(TestCase):
    def setUp(self) -> None:
        self.profile = _make_profile()
        self.session = _make_session(self.profile)

    def test_saves_the_message(self) -> None:
        message = send_chat_message(self.session, self.profile, "hello there")
        self.assertEqual(message.body, "hello there")
        self.assertEqual(message.profile_id, self.profile.pk)

    def test_strips_and_truncates_the_body(self) -> None:
        message = send_chat_message(self.session, self.profile, "  " + ("x" * (MAX_MESSAGE_LENGTH + 50)) + "  ")
        self.assertEqual(len(message.body), MAX_MESSAGE_LENGTH)

    @patch("urbanlens.dashboard.services.spotguessr.realtime.broadcast")
    def test_broadcasts_the_message(self, mock_broadcast) -> None:
        message = send_chat_message(self.session, self.profile, "hello")
        mock_broadcast.assert_called_once()
        session_id_arg, event_type_arg, payload_arg = mock_broadcast.call_args.args
        self.assertEqual(session_id_arg, self.session.pk)
        self.assertEqual(event_type_arg, "chat.message")
        self.assertEqual(payload_arg["message"]["message_id"], message.pk)


class RecentMessagesTests(TestCase):
    def setUp(self) -> None:
        self.profile = _make_profile()
        self.session = _make_session(self.profile)

    def test_returns_messages_oldest_first(self) -> None:
        first = send_chat_message(self.session, self.profile, "first")
        second = send_chat_message(self.session, self.profile, "second")
        messages = recent_messages(self.session)
        self.assertEqual([m.pk for m in messages], [first.pk, second.pk])

    def test_respects_the_limit_keeping_the_most_recent(self) -> None:
        for i in range(5):
            send_chat_message(self.session, self.profile, f"message {i}")
        messages = recent_messages(self.session, limit=2)
        self.assertEqual([m.body for m in messages], ["message 3", "message 4"])

    def test_only_returns_messages_for_this_session(self) -> None:
        other_session = _make_session(self.profile)
        send_chat_message(other_session, self.profile, "not mine")
        send_chat_message(self.session, self.profile, "mine")
        messages = recent_messages(self.session)
        self.assertEqual([m.body for m in messages], ["mine"])

    def test_default_limit_matches_the_module_constant(self) -> None:
        self.assertEqual(CHAT_HISTORY_LIMIT, 50)
