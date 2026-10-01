"""Active participation in Trivia sessions: invited or joined, never departed."""

from __future__ import annotations

from urbanlens.dashboard.models.trivia.model import TriviaSession, TriviaSessionParticipant
from urbanlens.dashboard.services.core.session_access import SessionAccess

session_access: SessionAccess[TriviaSession] = SessionAccess(name="trivia", session_model=TriviaSession, participants=TriviaSessionParticipant.objects)
