"""Undo-history controller: settings-page listing, stack undo/redo, restore, and clear-all."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views import View

from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.undo import UndoAction
from urbanlens.dashboard.services.undo.service import (
    NothingToUndoError,
    UndoExpiredError,
    clear_undo_history,
    get_undo_history,
    redo_latest,
    restore_undo_action,
    stack_state,
    undo_latest,
)

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse

logger = logging.getLogger(__name__)

_PARTIAL = "dashboard/partials/undo/undo_history.html"


def _request_profile(request: HttpRequest) -> Profile:
    profile, _ = Profile.objects.get_or_create(user=request.user)
    return profile


def _with_toast(response: HttpResponse, message: str, level: str = "success") -> HttpResponse:
    triggers = json.loads(response.headers.get("HX-Trigger", "{}")) if response.headers.get("HX-Trigger") else {}
    triggers["showToast"] = {"level": level, "message": message}
    response["HX-Trigger"] = json.dumps(triggers)
    return response


def _stack_response(profile: Profile, *, ok: bool = True, error: str | None = None, status: int = 200) -> JsonResponse:
    payload = stack_state(profile)
    payload["ok"] = ok
    if error:
        payload["error"] = error
    return JsonResponse(payload, status=status)


class UndoHistoryView(LoginRequiredMixin, View):
    """GET /settings/undo-history/ - HTMX partial listing a profile's undo history."""

    def get(self, request: HttpRequest) -> HttpResponse:
        profile = _request_profile(request)
        return render(request, _PARTIAL, {"actions": list(get_undo_history(profile))})


class UndoStackView(LoginRequiredMixin, View):
    """GET /undo/stack/ - whether the floating undo/redo buttons should show."""

    def get(self, request: HttpRequest) -> JsonResponse:
        return _stack_response(_request_profile(request))


class UndoPerformView(LoginRequiredMixin, View):
    """POST /undo/undo/ - undo the newest entry on the caller's stack."""

    def post(self, request: HttpRequest) -> JsonResponse:
        profile = _request_profile(request)
        try:
            undo_latest(profile)
        except NothingToUndoError:
            return _stack_response(profile, ok=False, error="Nothing to undo.", status=409)
        except UndoExpiredError:
            return _stack_response(profile, ok=False, error="That undo has expired.", status=410)
        return _stack_response(profile)


class UndoRedoView(LoginRequiredMixin, View):
    """POST /undo/redo/ - redo the most recently undone entry."""

    def post(self, request: HttpRequest) -> JsonResponse:
        profile = _request_profile(request)
        try:
            redo_latest(profile)
        except NothingToUndoError:
            return _stack_response(profile, ok=False, error="Nothing to redo.", status=409)
        except UndoExpiredError:
            return _stack_response(profile, ok=False, error="That redo has expired.", status=410)
        return _stack_response(profile)


class UndoRestoreView(LoginRequiredMixin, View):
    """POST /undo/<uuid:undo_id>/restore/ - restore one undo entry, from anywhere in the app."""

    def post(self, request: HttpRequest, undo_id) -> HttpResponse:
        profile = _request_profile(request)
        undo_action = get_object_or_404(UndoAction.objects.for_profile(profile), uuid=undo_id)

        try:
            restore_undo_action(undo_action)
        except UndoExpiredError:
            response = render(request, _PARTIAL, {"actions": list(get_undo_history(profile))})
            return _with_toast(response, "That undo has expired.", level="error")

        response = render(request, _PARTIAL, {"actions": list(get_undo_history(profile))})
        return _with_toast(response, "Restored.")


class UndoClearView(LoginRequiredMixin, View):
    """POST /settings/undo-history/clear/ - clear a profile's entire undo history."""

    def post(self, request: HttpRequest) -> HttpResponse:
        profile = _request_profile(request)
        count = clear_undo_history(profile)
        message = f"Cleared {count} undo entr{'y' if count == 1 else 'ies'}." if count else "Undo history is already empty."
        response = render(request, _PARTIAL, {"actions": []})
        return _with_toast(response, message)
