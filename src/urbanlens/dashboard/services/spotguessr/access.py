"""Active participation in SpotGuessr sessions."""

from __future__ import annotations

from urbanlens.dashboard.models.spotguessr.model import GameSession, GameSessionParticipant
from urbanlens.dashboard.services.core.session_access import SessionAccess

session_access: SessionAccess[GameSession] = SessionAccess(name="spotguessr", session_model=GameSession, participants=GameSessionParticipant.objects)
