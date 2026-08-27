"""Channel-layer group naming and broadcast for participant-session sockets.

SpotGuessr, Trivia and Consensus each had their own ``realtime`` module whose only
material difference was the group-name prefix - the docstrings themselves said "mirrors
``services.spotguessr.realtime`` exactly". The consumer side was already shared
(``consumers._ParticipantSessionConsumer``); this is the service-side counterpart, so a
change to how sessions broadcast is made once rather than three times.

Each game keeps its own ``realtime`` module as a thin binding, so existing callers and
their import paths are unchanged.
"""

from __future__ import annotations

from typing import Any

from urbanlens.dashboard.services.core.channel_broadcast import send_group_message


class SessionBroadcaster:
    """Broadcast helper for one game's per-session channel-layer group.

    Args:
        prefix: Game identifier used to build group names (e.g. ``"spotguessr"``).
            Must stay stable - it is the name connected consumers have already
            joined, so changing it orphans every open socket.
    """

    def __init__(self, prefix: str) -> None:
        self.prefix = prefix

    def session_group_name(self, session_id: int) -> str:
        """The channel-layer group every participant's WebSocket joins for one session.

        Args:
            session_id: Primary key of the session.

        Returns:
            The group name, namespaced by this broadcaster's game prefix.
        """
        return f"{self.prefix}_session_{session_id}"

    def broadcast(self, session_id: int, event_type: str, payload: dict[str, Any]) -> None:
        """Send ``payload`` to every connected participant of ``session_id``.

        A no-op (not an error) when no channel layer is configured, matching
        ``send_group_message``'s tolerance of a channel-layer-less environment.

        Args:
            session_id: Primary key of the session to broadcast to.
            event_type: Dot-notation Channels event type (e.g. ``"round.revealed"``),
                dispatched by the consumer to the matching ``round_revealed`` handler.
            payload: Event body, merged into the message alongside ``type``.
        """
        send_group_message(self.session_group_name(session_id), {"type": event_type, **payload})
