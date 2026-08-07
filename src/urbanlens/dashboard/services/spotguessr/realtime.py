"""Broadcast helpers for GameSessionConsumer's channel-layer group.

See ``docs/designs/drafts/spotguessr.md`` ("Real-time sync: GameSessionConsumer")
for the event catalogue this feeds. Kept separate from
``services.spotguessr.session``/``chat`` so both can call it without either
depending on the other.
"""

from __future__ import annotations

from typing import Any

from urbanlens.dashboard.services.core.channel_broadcast import send_group_message


def session_group_name(session_id: int) -> str:
    """The channel-layer group every participant's WebSocket joins for one session."""
    return f"spotguessr_session_{session_id}"


def broadcast(session_id: int, event_type: str, payload: dict[str, Any]) -> None:
    """Send ``payload`` to every connected participant of ``session_id``.

    A no-op (not an error) when no channel layer is configured - mirrors
    the existing ``push_notification_to_browser`` signal handler's
    tolerance of a channel-layer-less environment (e.g. some test setups).
    ``event_type`` is a dot-notation Channels event type (e.g.
    ``"round.revealed"``), dispatched by the consumer to the matching
    ``round_revealed`` handler method.
    """
    send_group_message(session_group_name(session_id), {"type": event_type, **payload})
