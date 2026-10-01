"""Broadcast helpers for GameSessionConsumer's channel-layer group.
Binds ``services.core.session_realtime.SessionBroadcaster``, shared by every participant-session game."""

from __future__ import annotations

from urbanlens.dashboard.services.core.session_realtime import SessionBroadcaster

#: The prefix is part of the group name connected consumers have already joined -
#: changing it orphans every open socket.
broadcaster = SessionBroadcaster("spotguessr")

session_group_name = broadcaster.session_group_name
broadcast = broadcaster.broadcast
