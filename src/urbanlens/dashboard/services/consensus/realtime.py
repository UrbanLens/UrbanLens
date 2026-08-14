"""Broadcast helpers for ConsensusSessionConsumer's channel-layer group.

Binds ``services.core.session_realtime.SessionBroadcaster``, shared by every
participant-session game. Kept as its own module so ``services.consensus.session`` and
``services.consensus.chat`` can both use it without either depending on the other, and so
existing import paths keep working.
"""

from __future__ import annotations

from urbanlens.dashboard.services.core.session_realtime import SessionBroadcaster

#: The prefix is part of the group name connected consumers have already joined -
#: changing it orphans every open socket.
broadcaster = SessionBroadcaster("consensus")

session_group_name = broadcaster.session_group_name
broadcast = broadcaster.broadcast
