"""External-facing AI assistant endpoints: a stateless mirror of the web chat.

``run_assistant_turn`` (``services.ai.assistant``) already takes conversation
history as an explicit parameter and has no dependency on Django's session -
the web view merely *chooses* to source/store history there. A bearer-token
client has no session to keep history in, so this domain has the client carry
its own history in the request body and resend the updated history this
endpoint returns on its next call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from drf_spectacular.utils import extend_schema
from rest_framework.response import Response

from urbanlens.dashboard.external_api.serializers import ErrorSerializer
from urbanlens.dashboard.external_api.serializers_assistant import (
    AssistantMessageRequestSerializer,
    AssistantMessageResponseSerializer,
    AssistantResetResponseSerializer,
)
from urbanlens.dashboard.external_api.throttling import AssistantMessageThrottle, ExternalApiBurstThrottle, ExternalApiReadThrottle, ExternalApiWriteThrottle
from urbanlens.dashboard.external_api.views import ExternalApiView
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.services.ai.assistant import MAX_HISTORY_ENTRIES, AssistantUnavailableError, run_assistant_turn

if TYPE_CHECKING:
    from rest_framework.request import Request


class AssistantMessageView(ExternalApiView):
    """POST: send one chat message and get the assistant's reply.

    Stateless: ``history`` is round-tripped through the client rather than
    kept server-side. Entries are capped to ``MAX_HISTORY_ENTRIES`` here (the
    same cap the web chat's session enforces); ``run_assistant_turn`` itself
    additionally caps the serialized transcript to ``MAX_HISTORY_CHARS``.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.ASSISTANT_WRITE}),
    }
    #: The standard three plus the assistant-specific cap - a chat turn still
    #: counts against the burst and write budgets as well. See
    #: throttling.AssistantMessageThrottle for why a turn needs its own cap.
    throttle_classes: ClassVar[list] = [ExternalApiBurstThrottle, ExternalApiReadThrottle, ExternalApiWriteThrottle, AssistantMessageThrottle]

    @extend_schema(request=AssistantMessageRequestSerializer, responses={200: AssistantMessageResponseSerializer, 400: ErrorSerializer, 503: ErrorSerializer})
    def post(self, request: Request) -> Response:
        """Process one chat turn and return the reply plus the history to resend next time."""
        serializer = AssistantMessageRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        profile = request.user.profile

        history = [dict(entry) for entry in data["history"]][-MAX_HISTORY_ENTRIES:]
        try:
            turn = run_assistant_turn(profile, history, data["message"])
        except AssistantUnavailableError:
            return Response({"error": "AI features are currently turned off for your account or this site."}, status=503)

        history.append({"role": "user", "content": data["message"]})
        history.append({"role": "assistant", "content": turn.reply})
        history = history[-MAX_HISTORY_ENTRIES:]
        return Response(AssistantMessageResponseSerializer({"reply": turn.reply, "actions": turn.actions, "history": history}).data)


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
