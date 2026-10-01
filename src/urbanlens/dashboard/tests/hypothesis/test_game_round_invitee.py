"""An invitee who never joined cannot create, advance or complete a game's rounds through the round view (N29 batch 1).

The round GET creates the next round once the last is revealed, and completes the session when none is left. Each
game's active participants include INVITED rows, so without a joined check an invitee drove both for the players.
"""

from __future__ import annotations

from unittest import mock

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.features import grant_alpha_features
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.consensus.model import (
    ConsensusSession,
    ConsensusSessionParticipant,
    ConsensusSessionParticipantStatus,
)
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.spotguessr.model import (
    GameSession,
    GameSessionParticipant,
    GameSessionParticipantStatus,
    SpotGuessrMode,
)
from urbanlens.dashboard.models.trivia.model import (
    TriviaSession,
    TriviaSessionParticipant,
    TriviaSessionParticipantStatus,
)


def _profile() -> Profile:
    user = baker.make("auth.User")
    grant_alpha_features(user)
    return Profile.objects.get(user=user)


class _RoundViewInviteeCase:
    """One game's round view, driven by a joined host and an invitee who never accepted."""

    route: str
    service: str
    session_model: type
    participant_model: type
    joined: str
    invited: str
    session_kwargs: dict = {}

    def setUp(self) -> None:
        super().setUp()  # type: ignore[misc]
        self.host = _profile()
        self.invitee = _profile()
        self.session = baker.make(self.session_model, host_profile=self.host, status="active", **self.session_kwargs)
        baker.make(self.participant_model, session=self.session, profile=self.host, status=self.joined)
        baker.make(self.participant_model, session=self.session, profile=self.invitee, status=self.invited)

    def _get(self, profile: Profile):
        self.client.force_login(profile.user)  # type: ignore[attr-defined]
        with (
            mock.patch(f"{self.service}.get_or_create_round", return_value=None) as create,
            mock.patch(f"{self.service}.rounds_played", return_value=3),
            mock.patch(f"{self.service}.complete_session") as complete,
            mock.patch(f"{self.service}.session_summary", return_value={}),
        ):
            response = self.client.get(reverse(self.route, kwargs={"session_id": self.session.pk}))  # type: ignore[attr-defined]
        return response, create, complete

    def test_an_invitee_neither_creates_nor_completes_a_round(self) -> None:
        response, create, complete = self._get(self.invitee)

        self.assertEqual(response.status_code, 403)  # type: ignore[attr-defined]
        create.assert_not_called()
        complete.assert_not_called()

    def test_a_joined_player_still_advances_the_game(self) -> None:
        response, create, complete = self._get(self.host)

        self.assertEqual(response.status_code, 200)  # type: ignore[attr-defined]
        create.assert_called_once()
        complete.assert_called_once()


class SpotGuessrRoundInviteeTests(_RoundViewInviteeCase, TestCase):
    route = "spotguessr.round"
    service = "urbanlens.dashboard.services.spotguessr.session"
    session_model = GameSession
    participant_model = GameSessionParticipant
    joined = GameSessionParticipantStatus.JOINED
    invited = GameSessionParticipantStatus.INVITED
    session_kwargs = {"mode": SpotGuessrMode.PHOTOS}


class TriviaRoundInviteeTests(_RoundViewInviteeCase, TestCase):
    route = "trivia.round"
    service = "urbanlens.dashboard.services.trivia.session"
    session_model = TriviaSession
    participant_model = TriviaSessionParticipant
    joined = TriviaSessionParticipantStatus.JOINED
    invited = TriviaSessionParticipantStatus.INVITED


class ConsensusRoundInviteeTests(_RoundViewInviteeCase, TestCase):
    route = "consensus.round"
    service = "urbanlens.dashboard.services.consensus.session"
    session_model = ConsensusSession
    participant_model = ConsensusSessionParticipant
    joined = ConsensusSessionParticipantStatus.JOINED
    invited = ConsensusSessionParticipantStatus.INVITED
