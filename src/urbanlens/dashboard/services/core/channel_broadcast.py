"""Shared entry point for pushing channel-layer group messages from sync code."""

from __future__ import annotations

from typing import Any

from channels.layers import get_channel_layer


def send_group_message(group: str, message: dict[str, Any]) -> None:
    """Best-effort delivery of ``message`` to every channel in ``group``.
    Never raises - a broker or channel-layer failure is logged by the task/enqueue helper, not surfaced here, matching every caller's existing "already durably saved, live delivery is a bonus" contract.

    Args:
        group: Channel-layer group name to deliver to.
        message: JSON-serializable event dict (must include a "type" key)."""
    if get_channel_layer() is None:
        return

    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.tasks import broadcast_channel_group_message

    safely_enqueue_task(broadcast_channel_group_message, group, message)
