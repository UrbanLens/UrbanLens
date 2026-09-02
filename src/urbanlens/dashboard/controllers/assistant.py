"""Assistant controller - the chat UI over the async turn task (UL-293).

Conversation state lives in the session (per browser, capped), not in a
conversation/message table of its own: the chat is a scratchpad, not a
record, so there's nothing to export or retain deliberately. The session
backend is ``cached_db``, which writes through to the database, so this
state does land in a DB row (``django_session``) - it just isn't modeled,
queryable, or kept past the session's own expiry.

A turn now runs on ``ai-worker`` (``services.ai.tasks.run_assistant_turn_task``),
never inline in this process - this view gates, enqueues, and polls, the
same shape as every other slow background operation in this app (see e.g.
``controllers.immich``'s library-scan progress view). One session entry
represents an in-flight turn until its poll resolves it -
``{"role": "assistant", "pending": True, "turn_id": ...}`` - and
:func:`_history` is the single point that repairs a stale one (the server
restarted, the turn record's TTL lapsed, the cache was flushed) into an
error bubble, so a reopened tab never polls a turn_id that will never answer.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.views import View

from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.ai.access import assistant_available
from urbanlens.dashboard.services.ai.assistant import MAX_HISTORY_ENTRIES, MAX_MESSAGE_CHARS
from urbanlens.dashboard.services.ai.turns import (
    MAX_POLL_ATTEMPTS,
    acquire_turn_lock,
    claim_turn_proposal,
    new_turn_id,
    read_turn_proposal,
    read_turn_record,
    release_turn_lock,
    store_turn_proposals,
    store_turn_record,
    turn_poll_delay,
)
from urbanlens.dashboard.services.core.celery import get_task_progress, safely_enqueue_task

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = logging.getLogger(__name__)

_SESSION_KEY = "assistant_chat"
_MESSAGES_PARTIAL = "dashboard/partials/assistant/_messages.html"
_BUBBLE_PARTIAL = "dashboard/partials/assistant/_bubble.html"
_PROPOSAL_PARTIAL = "dashboard/partials/assistant/_proposal.html"
#: Every reply the web tier itself can produce without ever reaching the
#: task - the task's own equivalents (services.ai.tasks) are separate
#: strings so the two layers stay decoupled.
_UNAVAILABLE_REPLY = "AI features are currently turned off for your account or this site."
_BUSY_MESSAGE = "Still working on your last message - hold on a moment."
_QUEUE_FAILED_REPLY = "Couldn't reach the assistant just now. Please try again."
_EXPIRED_REPLY = "This request expired before it finished. Please try again."
_GAVE_UP_REPLY = "This is taking longer than expected. Please try again in a moment."
#: How long a resolved turn's consume-gate marker lives - only needs to
#: outlast two browser tabs racing to render the same poll response, not the
#: turn record itself.
_CONSUME_GATE_TTL_SECONDS = 900


def _history(request: HttpRequest) -> list[dict[str, Any]]:
    """The session's conversation, with any turn whose record has expired resolved to an error bubble."""
    entries = request.session.get(_SESSION_KEY) or []
    if not isinstance(entries, list):
        return []
    changed = False
    for entry in entries:
        if entry.get("pending") and read_turn_record(entry.get("turn_id", "")) is None:
            entry.clear()
            entry.update({"role": "assistant", "content": _EXPIRED_REPLY, "actions": []})
            changed = True
    if changed:
        _save_history(request, entries)
    return entries


def _save_history(request: HttpRequest, entries: list[dict[str, Any]]) -> None:
    request.session[_SESSION_KEY] = entries[-MAX_HISTORY_ENTRIES:]
    request.session.modified = True


def _messages_context(request: HttpRequest) -> dict[str, Any]:
    return {"messages_history": _history(request), "first_poll_delay": turn_poll_delay(0)}


def _poll_attempt(request: HttpRequest) -> int:
    """Which poll cycle this request is (0 for the initial render)."""
    try:
        return max(int(request.GET.get("attempt", "0")), 0)
    except (TypeError, ValueError):
        return 0


def _resolve_turn(request: HttpRequest, turn_id: str, final_entry: dict[str, Any]) -> dict[str, Any]:
    """Write ``final_entry`` into session history in place of the pending marker for ``turn_id``, once.

    Consume-gated (``cache.add``) so two browser tabs polling the same turn
    both render the same final content, but only the first to arrive here
    appends it to history - the second is a no-op past the gate.

    Args:
        request: The current request (its session is what gets mutated).
        turn_id: The turn being resolved.
        final_entry: The entry to store in place of the pending marker.

    Returns:
        ``final_entry`` with ``turn_id`` set - the caller's own render needs
        it too (a proposal's confirm button builds its URL from
        ``entry.turn_id``), not just the session copy, and the pending
        marker's own ``turn_id`` would otherwise be lost the moment
        ``entry.clear()`` wipes it below.
    """
    final_entry = {**final_entry, "turn_id": turn_id}
    if not cache.add(f"ulai:turn:{turn_id}:consumed", 1, _CONSUME_GATE_TTL_SECONDS):
        return final_entry
    history = _history(request)
    for entry in history:
        if entry.get("turn_id") == turn_id:
            entry.clear()
            entry.update(final_entry)
            break
    _save_history(request, history)
    return final_entry


def _session_proposals(proposals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The session-visible shape of a turn's proposals - never ``args``.

    ``args`` only ever needs to be read back by the confirm view, from the
    cache-backed store (``store_turn_proposals``) - the session copy exists
    purely to render a confirm button (or its resolved state) and has no
    reason to carry it.
    """
    return [{"n": proposal["n"], "tool": proposal["tool"], "confirm_label": proposal["confirm_label"], "status": "pending"} for proposal in proposals]


def _update_session_proposal(request: HttpRequest, turn_id: str, n: int, *, status: str, message: str) -> None:
    """Mark proposal ``n`` of ``turn_id`` resolved in session, so a reload doesn't offer it again."""
    history = _history(request)
    for entry in history:
        if entry.get("turn_id") != turn_id:
            continue
        for proposal in entry.get("proposals") or []:
            if proposal.get("n") == n:
                proposal["status"] = status
                proposal["message"] = message
        break
    _save_history(request, history)


class AssistantView(LoginRequiredMixin, View):
    """The assistant page.

    GET /assistant/
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        return render(
            request,
            "dashboard/pages/assistant/index.html",
            {
                "page_name": "assistant",
                "profile": profile,
                "assistant_enabled": assistant_available(profile),
                "max_message_chars": MAX_MESSAGE_CHARS,
                **_messages_context(request),
            },
        )


class AssistantMessageView(LoginRequiredMixin, View):
    """Enqueue one chat message and return the log with a pending bubble for it.

    POST /assistant/message/   body: ``message``
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        message = (request.POST.get("message") or "").strip()[:MAX_MESSAGE_CHARS]
        if not message:
            return render(request, _MESSAGES_PARTIAL, _messages_context(request))

        history = _history(request)

        if not assistant_available(profile):
            history.append({"role": "user", "content": message})
            history.append({"role": "assistant", "content": _UNAVAILABLE_REPLY, "actions": []})
            _save_history(request, history)
            return render(request, _MESSAGES_PARTIAL, _messages_context(request))

        lock_token = acquire_turn_lock(profile)
        if lock_token is None:
            # A turn is already in flight for this profile - the message is
            # dropped rather than queued behind it (see turns.py's own
            # single-flight docstring); the pending bubble already in
            # history keeps polling on its own regardless.
            response = render(request, _MESSAGES_PARTIAL, _messages_context(request))
            response["HX-Trigger"] = json.dumps({"showToast": {"level": "info", "message": _BUSY_MESSAGE}})
            return response

        from urbanlens.dashboard.services.ai.tasks import run_assistant_turn_task

        # Prior turns only - the new message is a separate argument to the
        # task, matching run_assistant_turn's own (history, user_message)
        # split. Pending markers carry no "content" and would corrupt the
        # transcript the task builds.
        history_for_task = [{"role": entry["role"], "content": entry["content"]} for entry in history if not entry.get("pending")]
        result = safely_enqueue_task(run_assistant_turn_task, profile.pk, history_for_task, message, lock_token, expires=120)
        if result is None:
            release_turn_lock(profile, lock_token)
            history.append({"role": "user", "content": message})
            history.append({"role": "assistant", "content": _QUEUE_FAILED_REPLY, "actions": []})
            _save_history(request, history)
            return render(request, _MESSAGES_PARTIAL, _messages_context(request))

        turn_id = new_turn_id()
        store_turn_record(turn_id, profile_id=profile.pk, task_id=str(result.id), lock_token=lock_token)
        history.append({"role": "user", "content": message})
        history.append({"role": "assistant", "pending": True, "turn_id": turn_id})
        _save_history(request, history)
        return render(request, _MESSAGES_PARTIAL, _messages_context(request))


class AssistantTurnPollView(LoginRequiredMixin, View):
    """Poll one in-flight turn; renders just that one bubble (outerHTML swap).

    GET /assistant/turn/<turn_id>/
    """

    def get(self, request: HttpRequest, turn_id: str) -> HttpResponse:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        record = read_turn_record(turn_id)
        if record is None or record.get("profile_id") != profile.pk:
            # Unknown to this cache, or someone else's turn - identical
            # response either way, so a guessed turn_id can't distinguish
            # "expired" from "not yours".
            raise Http404

        attempt = _poll_attempt(request)
        if attempt >= MAX_POLL_ATTEMPTS:
            entry = _resolve_turn(request, turn_id, {"role": "assistant", "content": _GAVE_UP_REPLY, "actions": []})
            return render(request, _BUBBLE_PARTIAL, {"entry": entry})

        progress = get_task_progress(record["task_id"])
        if progress.state == "SUCCESS":
            from celery.result import AsyncResult

            result = progress.result if isinstance(progress.result, dict) else {}
            proposals = result.get("proposals") or []
            if proposals:
                store_turn_proposals(turn_id, profile_id=profile.pk, proposals=proposals)
            entry = _resolve_turn(
                request,
                turn_id,
                {"role": "assistant", "content": result.get("reply", ""), "actions": result.get("actions", []), "proposals": _session_proposals(proposals)},
            )
            AsyncResult(record["task_id"]).forget()
            return render(request, _BUBBLE_PARTIAL, {"entry": entry})
        if progress.state in {"FAILURE", "REVOKED"}:
            from celery.result import AsyncResult

            entry = _resolve_turn(request, turn_id, {"role": "assistant", "content": _EXPIRED_REPLY, "actions": []})
            AsyncResult(record["task_id"]).forget()
            return render(request, _BUBBLE_PARTIAL, {"entry": entry})

        return render(request, _BUBBLE_PARTIAL, {"entry": {"pending": True, "turn_id": turn_id}, "next_attempt": attempt + 1, "poll_delay": turn_poll_delay(attempt)})


class AssistantProposalConfirmView(LoginRequiredMixin, View):
    """Confirm (and actually run) one write-tool proposal from a resolved turn.

    The write itself never ran inside the turn loop - it ran on ai-worker,
    where ``registry.execute()`` refuses every write outright, and even
    off ai-worker the loop always calls ``execute(..., confirmed=False)``.
    This view is that write's only real execution path: it runs here, on
    the ordinary web process, only once the user has explicitly clicked
    confirm.

    POST /assistant/turn/<turn_id>/confirm/<n>/
    """

    def post(self, request: HttpRequest, turn_id: str, n: int) -> HttpResponse:
        from django.utils import timezone

        from urbanlens.dashboard.services.ai.tools.registry import ToolContext, execute

        profile, _ = Profile.objects.get_or_create(user=request.user)
        proposal = read_turn_proposal(turn_id, n)
        if proposal is None or proposal.get("profile_id") != profile.pk:
            # Unknown, expired, or someone else's - identical response
            # either way, matching every other turn/proposal lookup here.
            raise Http404

        if not claim_turn_proposal(turn_id, n):
            # Already confirmed (a double click, a retried request) - render
            # whatever the earlier confirm already recorded, never run twice.
            # Only trust that stored copy once it's actually resolved: the
            # winner claims first and only writes "done"/"error" back to the
            # session *after* execute() returns, so a loser arriving in that
            # gap would otherwise re-render a still-"pending" proposal here -
            # with no ``entry`` in this render's context, _proposal.html's
            # confirm button would NoReverseMatch on an empty turn_id.
            history = _history(request)
            for entry in history:
                if entry.get("turn_id") != turn_id:
                    continue
                for stored in entry.get("proposals") or []:
                    if stored.get("n") == n and stored.get("status") in {"done", "error"}:
                        return render(request, _PROPOSAL_PARTIAL, {"proposal": stored})
                break
            return render(request, _PROPOSAL_PARTIAL, {"proposal": {"n": n, "status": "done", "message": "Already confirmed."}})

        context = ToolContext(profile=profile, now=timezone.now())
        result = execute(proposal["tool"], proposal["args"], context, confirmed=True)
        is_error = "error" in result.data
        message = str(result.data.get("error")) if is_error else (result.summary or "Done.")
        status = "error" if is_error else "done"
        _update_session_proposal(request, turn_id, n, status=status, message=message)

        response = render(request, _PROPOSAL_PARTIAL, {"proposal": {"n": n, "status": status, "message": message}})
        if not is_error:
            response["HX-Trigger"] = json.dumps({"showToast": {"level": "success", "message": message}})
        return response


class AssistantResetView(LoginRequiredMixin, View):
    """Clear the conversation.

    A display-only reset: any turn already in flight keeps running on
    ai-worker and releases its own lock when it finishes - there is nothing
    here to cancel, only a client-side log to clear (same as before this
    view had anything async to worry about).

    POST /assistant/reset/
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        request.session.pop(_SESSION_KEY, None)
        request.session.modified = True
        return render(request, _MESSAGES_PARTIAL, {"messages_history": []})
