"""Celery entry point for the assistant's async turn.

Runs on :attr:`~services.sandbox.queues.Queue.AI`, drained only by
``ai-worker`` (``ProcessRole.AI``) - see that queue's own docstring for why
it has no default-queue fallback the way ``sandbox_queue()`` does. Starting
and polling a turn is the web view's/external API's job, using the
lock/record primitives in ``services.ai.turns``; this module is only the
task itself.
"""

from __future__ import annotations

import logging
from typing import Any

from celery import shared_task

from urbanlens.dashboard.services.ai.access import assistant_available
from urbanlens.dashboard.services.ai.assistant import AssistantUnavailableError, run_assistant_turn
from urbanlens.dashboard.services.ai.turns import release_turn_lock, turn_lock_is_current
from urbanlens.dashboard.services.core.celery import update_task_progress
from urbanlens.dashboard.services.sandbox.queues import ai_queue

logger = logging.getLogger(__name__)

# Read once at import time, matching sandbox_queue()'s own convention (see
# services/sandbox/queues.py) - Celery reads Task.queue through its exec
# options, so this is what makes every caller route here, including ones
# written later.
_AI_QUEUE = ai_queue()

_UNAVAILABLE_REPLY = "AI features are currently turned off for your account or this site."
_EXPIRED_REPLY = "This request took too long to start and was dropped. Please try again."


@shared_task(bind=True, queue=_AI_QUEUE, acks_late=False, soft_time_limit=90, time_limit=120)
def run_assistant_turn_task(self, profile_id: int, history: list[dict[str, Any]], user_message: str, lock_token: str, page: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run one assistant turn on ``ai-worker`` and release its single-flight lock.

    ``acks_late=False`` overrides the project default
    (``CELERY_TASK_ACKS_LATE = True``) deliberately: a turn that dies
    mid-loop must not be redelivered and re-spend a provider call for a
    bubble the caller has already timed out on.

    Args:
        profile_id: The requesting profile's id - re-fetched here rather
            than passed as a model instance, since Celery serializes task
            arguments.
        history: Prior conversation entries, already capped by the caller.
        user_message: The new message, already capped by the caller.
        lock_token: The single-flight lock token this turn holds
            (``services.ai.turns.acquire_turn_lock``). Checked against the
            profile's current lock before any provider call - a stale task
            whose lock has already expired and been re-acquired by a newer
            turn must not spend work on a bubble nothing is polling for.
            :func:`~services.ai.turns.release_turn_lock` is separately
            self-guarding, so even if that check were somehow bypassed the
            release below can't clobber a newer turn's lock.
        page: ``services.ai.page_context.page_object_to_dict``'s output for
            whatever the web view resolved before enqueueing, or ``None``.
            Re-verified against *this* task's own profile via
            ``verify_page_object`` before use - the web view's earlier
            resolution is never trusted as-is, only its ``{kind, id}``.

    Returns:
        ``{"reply": str, "actions": list[str], "proposals": list[dict]}`` on
        every handled path (unavailable, expired, or a real turn) - the poll
        endpoint always has something to show. ``proposals`` (from
        ``AssistantTurn.proposals``) are write tools the model asked for
        that did *not* run - see ``services.ai.tools.registry.execute``'s
        ``confirmed`` parameter; the poll endpoint that resolves this result
        is what persists them (``services.ai.turns.store_turn_proposals``)
        for a later confirm request to look up, since this task has no
        reason to know its own turn_id. An unhandled exception (including
        ``SoftTimeLimitExceeded`` if the in-loop deadline check in
        ``run_assistant_turn`` somehow doesn't catch a hang first) still
        propagates as a Celery ``FAILURE``, which the poll endpoint renders
        as its own error bubble.
    """
    from urbanlens.dashboard.models.profile.model import Profile

    profile = Profile.objects.filter(pk=profile_id).first()
    if profile is None:
        # Deleted between enqueue and pickup - vanishingly rare, and there is
        # no one left to poll for a reply. The lock's own TTL reclaims it;
        # not worth a lookup-then-release for a profile that no longer exists.
        return {"reply": _UNAVAILABLE_REPLY, "actions": [], "proposals": []}

    if not turn_lock_is_current(profile, lock_token):
        logger.info("Assistant turn for profile %s lost its lock before executing; skipping", profile_id)
        return {"reply": _EXPIRED_REPLY, "actions": [], "proposals": []}

    try:
        if not assistant_available(profile):
            return {"reply": _UNAVAILABLE_REPLY, "actions": [], "proposals": []}

        from urbanlens.dashboard.services.ai.page_context import page_object_from_dict, verify_page_object

        page_object = page_object_from_dict(page)
        if page_object is not None and not verify_page_object(profile, page_object):
            page_object = None

        update_task_progress(self, current=0, total=1, message="Thinking…")
        try:
            turn = run_assistant_turn(profile, history, user_message, page=page_object)
        except AssistantUnavailableError:
            return {"reply": _UNAVAILABLE_REPLY, "actions": [], "proposals": []}

        update_task_progress(self, current=1, total=1, message="Done")
        return {"reply": turn.reply, "actions": turn.actions, "proposals": turn.proposals}
    finally:
        release_turn_lock(profile, lock_token)
