"""Active participation in Consensus sessions."""

from __future__ import annotations

from urbanlens.dashboard.models.consensus.model import ConsensusSession, ConsensusSessionParticipant
from urbanlens.dashboard.services.core.session_access import SessionAccess

session_access: SessionAccess[ConsensusSession] = SessionAccess(name="consensus", session_model=ConsensusSession, participants=ConsensusSessionParticipant.objects)
