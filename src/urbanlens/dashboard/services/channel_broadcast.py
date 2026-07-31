"""Shared entry point for pushing channel-layer group messages from sync code.

Production runs gunicorn with the gevent worker class (see ``gunicorn.conf.py``):
many requests are cooperatively scheduled onto one shared OS thread per worker
process. Asyncio's "is a loop currently running" state - the exact thing
Django's ``SynchronousOnlyOperation`` check reads via
``asyncio.get_running_loop()`` - is tracked per OS thread, not per greenlet,
and gevent's monkeypatching has no way to virtualize that C-level state the
way it does e.g. ``threading.local``. Calling
``asgiref.sync.async_to_sync(channel_layer.group_send)`` inline during a
request therefore risks poisoning *any other* in-flight greenlet on the same
worker for the duration of the call - an unrelated concurrent request's
perfectly ordinary ORM call can trip ``SynchronousOnlyOperation`` (see
docs/PROBLEMS.md's gevent/asyncio entry for the production incident this
fixes). Routing the actual ``async_to_sync`` call through a Celery task
(``celery-worker``'s prefork pool - a real, separate OS process per slot)
sidesteps the incompatibility entirely.
"""

from __future__ import annotations

from typing import Any

from channels.layers import get_channel_layer


def send_group_message(group: str, message: dict[str, Any]) -> None:
    """Best-effort delivery of ``message`` to every channel in ``group``.

    A no-op when no channel layer is configured (mirrors every caller's
    existing tolerance of a channel-layer-less environment, e.g. some test
    setups). Never raises - a broker or channel-layer failure is logged by
    the task/enqueue helper, not surfaced here, matching every caller's
    existing "already durably saved, live delivery is a bonus" contract.

    Args:
        group: Channel-layer group name to deliver to.
        message: JSON-serializable event dict (must include a "type" key).
    """
    if get_channel_layer() is None:
        return

    from urbanlens.dashboard.services.celery import safely_enqueue_task
    from urbanlens.dashboard.tasks import broadcast_channel_group_message

    safely_enqueue_task(broadcast_channel_group_message, group, message)
