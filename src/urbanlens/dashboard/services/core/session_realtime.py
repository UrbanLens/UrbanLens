"""Channel-layer group naming and broadcast for participant-session sockets.
SpotGuessr, Trivia and Consensus each had their own ``realtime`` module whose only material difference was the group-name prefix - the docstrings themselves said "mirrors ``services.spotguessr.realtime`` exactly"."""

from __future__ import annotations

from typing import Any

from urbanlens.dashboard.services.core.channel_broadcast import send_group_message


class SessionBroadcaster:
    """Broadcast helper for one game's per-session channel-layer group.

    Args:
        prefix: Must stay stable - it is the name connected consumers have already joined, so changing it orphans every open socket."""

    def __init__(self, prefix: str) -> None:
        self.prefix = prefix

    def session_group_name(self, session_id: int) -> str:
        """The channel-layer group every participant's WebSocket joins for one session.

        Returns:
            The group name, namespaced by this broadcaster's game prefix."""
        return f"{self.prefix}_session_{session_id}"

    def broadcast(self, session_id: int, event_type: str, payload: dict[str, Any]) -> None:
        """Send ``payload`` to every connected participant of ``session_id``."""
        send_group_message(self.session_group_name(session_id), {"type": event_type, **payload})
