"""External-facing AI assistant endpoints: a stateless mirror of the web chat.

A turn runs on ``ai-worker`` (``services.ai.tasks.run_assistant_turn_task``),
never inline in this process - this domain gates, enqueues, and polls,
mirroring ``controllers.assistant``'s web flow. Statelessness is what
differs: the web view keeps conversation history in the Django session and
resolves a finished turn by mutating it in place; this domain has no
session, so the message/history that started a turn is cached under its own
key (:data:`_CONTEXT_TTL_SECONDS`) and read back once the poll resolves, to
build the ``history`` this endpoint hands back to the client for its next
call. Nothing here mutates shared state across two racing polls the way the
web view's consume gate must - ``get_task_progress``/cache reads are all
idempotent, so a second poll simply recomputes the same answer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from drf_spectacular.utils import extend_schema
from rest_framework.response import Response

from urbanlens.dashboard.external_api.serializers import ErrorSerializer
from urbanlens.dashboard.external_api.serializers_assistant import (
    AssistantMessageRequestSerializer,
    AssistantMessageResponseSerializer,
    AssistantResetResponseSerializer,
    AssistantTurnPendingSerializer,
)
from urbanlens.dashboard.external_api.throttling import AssistantMessageThrottle, ExternalApiBurstThrottle, ExternalApiReadThrottle, ExternalApiWriteThrottle
from urbanlens.dashboard.external_api.views import ExternalApiView
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.services.ai.access import assistant_available
from urbanlens.dashboard.services.ai.assistant import MAX_HISTORY_ENTRIES
from urbanlens.dashboard.services.ai.turns import MAX_POLL_ATTEMPTS, acquire_turn_lock, new_turn_id, read_turn_record, release_turn_lock, store_turn_record, turn_poll_delay
from urbanlens.dashboard.services.core.celery import get_task_progress, safely_enqueue_task

if TYPE_CHECKING:
    from rest_framework.request import Request

#: How long a turn's originating (message, history) stays cached for the poll
#: endpoint to read back - matches the turn record's own TTL (turns.py) so
#: neither expires first.
_CONTEXT_TTL_SECONDS = 15 * 60
_UNAVAILABLE_ERROR = "AI features are currently turned off for your account or this site."
_QUEUE_FAILED_ERROR = "Couldn't reach the assistant just now. Please try again."
_EXPIRED_REPLY = "This request expired before it finished. Please try again."
_GAVE_UP_REPLY = "This is taking longer than expected. Please try again in a moment."


def _context_key(turn_id: str) -> str:
    return f"ulai:turn:{turn_id}:api_ctx"


def _store_context(turn_id: str, *, message: str, history: list[dict[str, Any]]) -> None:
    from django.core.cache import cache

    cache.set(_context_key(turn_id), {"message": message, "history": history}, _CONTEXT_TTL_SECONDS)


def _read_context(turn_id: str) -> dict[str, Any]:
    from django.core.cache import cache

    context = cache.get(_context_key(turn_id))
    return context if isinstance(context, dict) else {"message": "", "history": []}


def _resolved_history(turn_id: str, reply: str) -> list[dict[str, Any]]:
    """The client's next ``history``: its own prior turns plus this one, capped."""
    context = _read_context(turn_id)
    history = [dict(entry) for entry in context.get("history") or []]
    history.append({"role": "user", "content": context.get("message", "")})
    history.append({"role": "assistant", "content": reply})
    return history[-MAX_HISTORY_ENTRIES:]


def _poll_attempt(request: Request) -> int:
    try:
        return max(int(request.query_params.get("attempt", "0")), 0)
    except (TypeError, ValueError):
        return 0


class AssistantMessageView(ExternalApiView):
    """POST: enqueue one chat message; 202 with a turn id to poll.

    Stateless: ``history`` is round-tripped through the client rather than
    kept server-side. Entries are capped to ``MAX_HISTORY_ENTRIES`` here (the
    same cap the web chat's session enforces).
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.ASSISTANT_WRITE}),
    }
    #: The standard three plus the assistant-specific cap - a chat turn still
    #: counts against the burst and write budgets as well. See
    #: throttling.AssistantMessageThrottle for why a turn needs its own cap.
    throttle_classes: ClassVar[list] = [ExternalApiBurstThrottle, ExternalApiReadThrottle, ExternalApiWriteThrottle, AssistantMessageThrottle]

    @extend_schema(
        request=AssistantMessageRequestSerializer,
        responses={202: AssistantTurnPendingSerializer, 400: ErrorSerializer, 409: ErrorSerializer, 503: ErrorSerializer},
    )
    def post(self, request: Request) -> Response:
        """Enqueue one chat turn and return a turn id to poll for its reply."""
        serializer = AssistantMessageRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        profile = request.user.profile

        if not assistant_available(profile):
            return Response({"error": _UNAVAILABLE_ERROR}, status=503)

        lock_token = acquire_turn_lock(profile)
        if lock_token is None:
            return Response({"error": "A previous message is still being processed. Wait for it to finish before sending another."}, status=409)

        from urbanlens.dashboard.services.ai.tasks import run_assistant_turn_task

        history = [dict(entry) for entry in data["history"]][-MAX_HISTORY_ENTRIES:]
        result = safely_enqueue_task(run_assistant_turn_task, profile.pk, history, data["message"], lock_token, expires=120)
        if result is None:
            release_turn_lock(profile, lock_token)
            return Response({"error": _QUEUE_FAILED_ERROR}, status=503)

        turn_id = new_turn_id()
        store_turn_record(turn_id, profile_id=profile.pk, task_id=str(result.id), lock_token=lock_token)
        _store_context(turn_id, message=data["message"], history=history)
        return Response(AssistantTurnPendingSerializer({"turn_id": turn_id, "ready": False, "poll_after_seconds": turn_poll_delay(0)}).data, status=202)


class AssistantTurnPollView(ExternalApiView):
    """GET: poll one turn started by ``AssistantMessageView``."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.ASSISTANT_WRITE}),
    }
    throttle_classes: ClassVar[list] = [ExternalApiBurstThrottle, ExternalApiReadThrottle]

    @extend_schema(responses={200: AssistantMessageResponseSerializer, 202: AssistantTurnPendingSerializer, 404: ErrorSerializer})
    def get(self, request: Request, turn_id: str) -> Response:
        """Return the turn's reply if ready, else 202 with a retry hint."""
        profile = request.user.profile
        record = read_turn_record(turn_id)
        if record is None or record.get("profile_id") != profile.pk:
            # Unknown to this cache, or someone else's turn - identical
            # response either way, matching this API's usual anti-enumeration policy.
            return Response({"error": "No such turn."}, status=404)

        attempt = _poll_attempt(request)
        if attempt >= MAX_POLL_ATTEMPTS:
            return Response(AssistantMessageResponseSerializer({"reply": _GAVE_UP_REPLY, "actions": [], "history": _resolved_history(turn_id, _GAVE_UP_REPLY)}).data)

        progress = get_task_progress(record["task_id"])
        if progress.state == "SUCCESS":
            from celery.result import AsyncResult

            result = progress.result if isinstance(progress.result, dict) else {}
            reply = result.get("reply", "")
            response = Response(AssistantMessageResponseSerializer({"reply": reply, "actions": result.get("actions", []), "history": _resolved_history(turn_id, reply)}).data)
            AsyncResult(record["task_id"]).forget()
            return response
        if progress.state in {"FAILURE", "REVOKED"}:
            from celery.result import AsyncResult

            response = Response(AssistantMessageResponseSerializer({"reply": _EXPIRED_REPLY, "actions": [], "history": _resolved_history(turn_id, _EXPIRED_REPLY)}).data)
            AsyncResult(record["task_id"]).forget()
            return response

        return Response(AssistantTurnPendingSerializer({"ready": False, "poll_after_seconds": turn_poll_delay(attempt)}).data, status=202)


class AssistantResetView(ExternalApiView):
    """POST: reset the conversation.

    A genuine no-op for this stateless shape - there is no server-side
    history left to clear once it lives client-side. Kept for surface
    symmetry with the web routes the mobile requirements named; a client
    "resets" by simply discarding its own ``history`` and sending an empty
    list on its next message.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.ASSISTANT_WRITE}),
    }

    @extend_schema(request=None, responses={200: AssistantResetResponseSerializer})
    def post(self, request: Request) -> Response:
        """Return an empty history - there is nothing server-side to clear."""
        return Response(AssistantResetResponseSerializer({"history": []}).data)
