"""Broadcast helpers for TriviaSessionConsumer's channel-layer group.

Mirrors ``services.spotguessr.realtime`` exactly. Kept separate from
``services.trivia.session``/``chat`` so both can call it without either
depending on the other.
"""

from __future__ import annotations

from typing import Any

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer


def session_group_name(session_id: int) -> str:
    """The channel-layer group every participant's WebSocket joins for one session."""
    return f"trivia_session_{session_id}"


def broadcast(session_id: int, event_type: str, payload: dict[str, Any]) -> None:
    """Send ``payload`` to every connected participant of ``session_id``.

    A no-op (not an error) when no channel layer is configured. ``event_type``
    is a dot-notation Channels event type (e.g. ``"round.revealed"``),
    dispatched by the consumer to the matching ``round_revealed`` handler
    method.
    """
    layer = get_channel_layer()
    if layer is None:
        return
    async_to_sync(layer.group_send)(session_group_name(session_id), {"type": event_type, **payload})
