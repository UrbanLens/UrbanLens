"""Shared entry point for pushing channel-layer group messages from sync code."""

from __future__ import annotations

from typing import Any

from channels.layers import get_channel_layer

from urbanlens.dashboard.services.core.celery import safely_enqueue_task


def send_group_message(group: str, message: dict[str, Any]) -> None:
    """Best-effort delivery of ``message`` to every channel in ``group``.
    Never raises - a broker or channel-layer failure is logged by the task/enqueue helper, not surfaced here, matching every caller's existing "already durably saved, live delivery is a bonus" contract.

    Args:
        group: Channel-layer group name to deliver to.
        message: JSON-serializable event dict (must include a "type" key)."""
    if get_channel_layer() is None:
        return

    from urbanlens.dashboard.tasks import broadcast_channel_group_message

    safely_enqueue_task(broadcast_channel_group_message, group, message)


def send_group_messages(deliveries: list[tuple[str, dict[str, Any]]]) -> None:
    """Best-effort delivery of many ``(group, message)`` pairs in one task.

    One message to a fifty-person group used to be fifty calls to
    :func:`send_group_message`, and so fifty Celery tasks - each building its own
    event loop and its own channel-layer connection to push a single frame. The
    payloads still differ per recipient (a message carries the sender's name,
    resolved through each viewer's own visibility); the delivery does not.

    Args:
        deliveries: ``(channel group name, event dict)`` pairs. Each event must
            carry a ``"type"`` key.
    """
    if not deliveries or get_channel_layer() is None:
        return

    from urbanlens.dashboard.tasks import broadcast_channel_group_messages

    safely_enqueue_task(broadcast_channel_group_messages, list(deliveries))
