"""Memories → Locations page: review queue for PinMergeSuggestion rows.

Suggestions are raised by whatever trigger detects two of a profile's own pins
are probably the same place (today, only
``services.apis.locations.legacy_cid_coordinate_fix``'s collision case) - this
controller only lets the owner accept or reject what was already raised; it
never creates a suggestion itself.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views import View

from urbanlens.dashboard.models.pin_merge_suggestions.model import PinMergeSuggestion
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.core.pagination import get_page
from urbanlens.dashboard.services.pins.pin_merge import MergeFieldConflict, PinMergeCollisionError, UnresolvedMergeConflictError, plan_merge_conflicts
from urbanlens.dashboard.services.pins.pin_merge_suggestions import accept_pin_merge_suggestion, reject_pin_merge_suggestion

if TYPE_CHECKING:
    from collections.abc import Iterable

    from django.db.models import QuerySet
    from django.http import HttpRequest

logger = logging.getLogger(__name__)

_QUEUE_PARTIAL = "dashboard/partials/memories/_pin_merge_suggestions_queue.html"
_CARD_PARTIAL = "dashboard/partials/memories/_pin_merge_suggestion_card.html"
_PAGE_SIZE = 12


def pending_merge_suggestions(profile: Profile) -> QuerySet[PinMergeSuggestion]:
    """The profile's pending merge suggestions, newest first.

    Args:
        profile: Owner whose pending suggestions to fetch.

    Returns:
        Newest-first queryset of pending rows, with both pins (and their
        locations) selected in one query.
    """
    return PinMergeSuggestion.objects.for_profile(profile).pending().select_related("pin_a", "pin_a__location", "pin_b", "pin_b__location", "suggested_survivor").order_by("-created")


def merge_suggestion_cards(suggestions: Iterable[PinMergeSuggestion]) -> list[dict[str, Any]]:
    """Build per-card template context, one entry per suggestion.

    Conflicts are computed fresh here (not stored on the suggestion) since
    they must reflect the two pins' *current* state, which can change between
    when the suggestion was raised and when it's reviewed. Only ever called
    with pending suggestions (see callers), so both pins are guaranteed set.

    Args:
        suggestions: Iterable of ``PinMergeSuggestion`` rows.

    Returns:
        List of ``{"suggestion": ..., "conflicts": [...]}`` dicts.
    """
    return [{"suggestion": suggestion, "conflicts": _conflicts_for(suggestion)} for suggestion in suggestions]


def _conflicts_for(suggestion: PinMergeSuggestion) -> list[MergeFieldConflict]:
    """plan_merge_conflicts for a suggestion's two pins - both must still be set (i.e. still pending)."""
    if suggestion.pin_a is None or suggestion.pin_b is None:
        raise ValueError(f"PinMergeSuggestion {suggestion.pk} is missing one of its pins")
    return plan_merge_conflicts(suggestion.pin_a, suggestion.pin_b)


def _toast(message: str, level: str = "success", *, status: int = 200, refresh_queue: bool = False, view_pin_url: str | None = None) -> HttpResponse:
    """Return an empty HTMX response that removes the swapped card and fires a toast.

    Mirrors ``controllers.pin_suggestions._toast``.

    Args:
        message: Toast body text (HTML-escaped by the caller if it embeds
            any dynamic value).
        level: toastr level ("success", "info", "warning", "error").
        status: HTTP status code for the (otherwise empty) response.
        refresh_queue: Whether to also fire the ``refreshQueue`` htmx event.
        view_pin_url: If set, appends a "View pin" link to the toast.
    """
    if view_pin_url:
        message += f' <a href="{view_pin_url}" class="toast-undo-btn">View pin</a>'
    triggers: dict[str, Any] = {"showToast": {"message": message, "level": level}}
    if refresh_queue:
        triggers["refreshQueue"] = True
    response = HttpResponse("", status=status)
    response["HX-Trigger"] = json.dumps(triggers)
    return response


class PinMergeSuggestionQueuePartialView(LoginRequiredMixin, View):
    """Just the merge-suggestion queue partial, re-fetched via the ``refreshQueue`` event.

    GET /memories/locations/merge/queue/
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        page_obj = get_page(request, pending_merge_suggestions(profile), _PAGE_SIZE)
        return render(
            request,
            _QUEUE_PARTIAL,
            {"merge_suggestion_cards": merge_suggestion_cards(page_obj.object_list), "page_obj": page_obj},
        )


class PinMergeSuggestionActionView(LoginRequiredMixin, View):
    """Accept or reject a single pin merge suggestion.

    POST /memories/locations/merge/<suggestion_id>/<action>/ where action is
    "accept" or "reject".

    Accept body: ``survivor_pk`` (optional - defaults to
    ``suggestion.suggested_survivor``), plus one ``resolution__<key>`` field
    per conflict rendered on the card (see ``services.pins.pin_merge.plan_merge_conflicts``).
    Missing resolutions re-render the card with the conflict form intact and
    an error toast, rather than failing with a 500.
    """

    def _get_suggestion(self, request: HttpRequest, suggestion_id: int) -> tuple[PinMergeSuggestion, Profile]:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        suggestion = get_object_or_404(
            PinMergeSuggestion.objects.select_related("pin_a", "pin_a__location", "pin_b", "pin_b__location", "suggested_survivor"),
            pk=suggestion_id,
        )
        if suggestion.profile_id != profile.pk:
            raise Http404
        return suggestion, profile

    def post(self, request: HttpRequest, suggestion_id: int, action: str) -> HttpResponse:
        if action not in {"accept", "reject"}:
            raise Http404
        suggestion, profile = self._get_suggestion(request, suggestion_id)
        if not suggestion.is_actionable:
            return _toast("That suggestion has already been handled.", "info", refresh_queue=True)

        if action == "reject":
            reject_pin_merge_suggestion(suggestion)
            return _toast("Merge suggestion dismissed.", "info", refresh_queue=True)

        raw_survivor = request.POST.get("survivor_pk", "")
        survivor_pk = int(raw_survivor) if raw_survivor.isdigit() else None

        conflicts = _conflicts_for(suggestion)
        resolutions: dict[str, int] = {}
        for conflict in conflicts:
            raw_resolution = request.POST.get(f"resolution__{conflict.key}", "")
            if raw_resolution.isdigit():
                resolutions[conflict.key] = int(raw_resolution)

        try:
            merged = accept_pin_merge_suggestion(suggestion, profile, survivor_pk=survivor_pk, resolutions=resolutions)
        except UnresolvedMergeConflictError:
            response = render(
                request,
                _CARD_PARTIAL,
                {"suggestion": suggestion, "conflicts": conflicts},
            )
            response["HX-Trigger"] = json.dumps({"showToast": {"message": "Please resolve every highlighted difference before merging.", "level": "error"}})
            return response
        except PinMergeCollisionError as exc:
            # Refused rather than "went wrong": the user can act on this one by
            # moving the blocking top-level pin first.
            response = render(request, _CARD_PARTIAL, {"suggestion": suggestion, "conflicts": conflicts})
            response["HX-Trigger"] = json.dumps({"showToast": {"message": exc.safe_message, "level": "error"}})
            return response
        except ValueError:
            response = render(request, _CARD_PARTIAL, {"suggestion": suggestion, "conflicts": conflicts})
            response["HX-Trigger"] = json.dumps({"showToast": {"message": "Something went wrong. Please try again.", "level": "error"}})
            return response

        view_pin_url = reverse("pin.details", args=[merged.slug or merged.uuid])
        return _toast(f"Pins merged into {merged.effective_name}.", refresh_queue=True, view_pin_url=view_pin_url)
