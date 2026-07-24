"""Assistant controller - the chat UI over services.ai.assistant (UL-293).

Conversation state lives in the session (per browser, capped), never in the
database: the chat is a scratchpad, not a record, and keeping it out of the DB
means there's nothing to leak, export, or retain.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import render
from django.views import View

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse

from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.ai.assistant import (
    MAX_HISTORY_ENTRIES,
    MAX_MESSAGE_CHARS,
    AssistantUnavailableError,
    run_assistant_turn,
)
from urbanlens.dashboard.services.ai.factory import get_gateway

logger = logging.getLogger(__name__)

_SESSION_KEY = "assistant_chat"
_MESSAGES_PARTIAL = "dashboard/partials/assistant/_messages.html"


def _history(request: HttpRequest) -> list[dict[str, Any]]:
    entries = request.session.get(_SESSION_KEY) or []
    return entries if isinstance(entries, list) else []


def _save_history(request: HttpRequest, entries: list[dict[str, Any]]) -> None:
    request.session[_SESSION_KEY] = entries[-MAX_HISTORY_ENTRIES:]
    request.session.modified = True


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
                "messages_history": _history(request),
                "assistant_enabled": get_gateway(profile=profile) is not None,
                "max_message_chars": MAX_MESSAGE_CHARS,
            },
        )


class AssistantMessageView(LoginRequiredMixin, View):
    """Handle one chat message and re-render the message log.

    POST /assistant/message/   body: ``message``
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        message = (request.POST.get("message") or "").strip()[:MAX_MESSAGE_CHARS]
        history = _history(request)

        if message:
            try:
                turn = run_assistant_turn(profile, history, message)
            except AssistantUnavailableError:
                turn = None
            history.append({"role": "user", "content": message})
            if turn is None:
                history.append({"role": "assistant", "content": "AI features are currently turned off for your account or this site.", "actions": []})
            else:
                history.append({"role": "assistant", "content": turn.reply, "actions": turn.actions})
            _save_history(request, history)

        return render(request, _MESSAGES_PARTIAL, {"messages_history": _history(request)})


class AssistantResetView(LoginRequiredMixin, View):
    """Clear the conversation.

    POST /assistant/reset/
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        request.session.pop(_SESSION_KEY, None)
        request.session.modified = True
        return render(request, _MESSAGES_PARTIAL, {"messages_history": []})
