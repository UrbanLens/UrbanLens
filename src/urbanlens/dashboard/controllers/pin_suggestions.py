"""Memories → Locations page: review queue for batch-scan pin suggestions.

Suggestions are produced in bulk by ``services.pin_suggestions.ingest_location_hits``
(called from the Immich full-library sweep and the Tools-page local folder scanner) -
this controller only lets the owner accept or reject what was already found; it never
triggers a scan itself.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views import View

from urbanlens.dashboard.models.immich.model import ImmichAccount
from urbanlens.dashboard.models.pin_suggestions.model import PinSuggestion, PinSuggestionStatus
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.apis.immich import ImmichGateway
from urbanlens.dashboard.services.celery import safely_enqueue_task
from urbanlens.dashboard.services.gateway import GatewayRequestError
from urbanlens.dashboard.services.memories.unlogged import unlogged_visited_pins
from urbanlens.dashboard.services.pagination import get_page
from urbanlens.dashboard.services.pin_suggestions import accept_pin_suggestion, reject_pin_suggestion

if TYPE_CHECKING:
    from django.db.models import QuerySet
    from django.http import HttpRequest

logger = logging.getLogger(__name__)

_QUEUE_PARTIAL = "dashboard/partials/memories/_pin_suggestions_queue.html"
_CARD_PARTIAL = "dashboard/partials/memories/_pin_suggestion_card.html"
_PAGE_SIZE = 12
_THUMBNAIL_CACHE_TTL = 60 * 60 * 24
_MAX_BULK_SUGGESTIONS = 200
_BULK_ACTIONS = [
    {"action": "accept", "icon": "check", "label": "Accept"},
    {"action": "reject", "icon": "cancel", "label": "Not a match"},
]


def _pending_suggestions(profile: Profile) -> QuerySet[PinSuggestion]:
    """Return the profile's pending suggestions, newest first."""
    return (
        PinSuggestion.objects.for_profile(profile)
        .pending()
        .select_related("pin", "pin__location")
        .prefetch_related("candidate_images")
        .order_by("-created")
    )


def _toast(message: str, level: str = "success", *, status: int = 200, refresh_queue: bool = False) -> HttpResponse:
    """Return an empty HTMX response that removes the swapped card and fires a toast.

    Mirrors ``controllers.photos._toast``.
    """
    triggers: dict[str, Any] = {"showToast": {"message": message, "level": level}}
    if refresh_queue:
        triggers["refreshQueue"] = True
    response = HttpResponse("", status=status)
    response["HX-Trigger"] = json.dumps(triggers)
    return response


class PinSuggestionQueueView(LoginRequiredMixin, View):
    """The Locations subpage of Memories - the batch-scan suggestion review queue.

    GET /memories/locations/
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        suggestions_qs = _pending_suggestions(profile)
        page_obj = get_page(request, suggestions_qs, _PAGE_SIZE)
        return render(
            request,
            "dashboard/pages/memories/locations.html",
            {
                "page_name": "memories",
                "suggestions": page_obj.object_list,
                "page_obj": page_obj,
                "unlogged_visits_count": len(unlogged_visited_pins(profile)),
                "pin_suggestions_count": page_obj.paginator.count,
                "bulk_actions": _BULK_ACTIONS,
            },
        )


class PinSuggestionQueuePartialView(LoginRequiredMixin, View):
    """Just the suggestion queue partial, re-fetched via the ``refreshQueue`` event.

    GET /memories/locations/queue/
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        page_obj = get_page(request, _pending_suggestions(profile), _PAGE_SIZE)
        return render(request, _QUEUE_PARTIAL, {"suggestions": page_obj.object_list, "page_obj": page_obj})


class PinSuggestionMapDataView(LoginRequiredMixin, View):
    """Lightweight JSON of every pending suggestion, for the review-queue map.

    GET /memories/locations/map-data/

    Unlike the card grid, the map always shows every pending suggestion
    regardless of which card page is showing - spatial browsing is the point.
    """

    def get(self, request: HttpRequest) -> JsonResponse:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        suggestions = _pending_suggestions(profile)
        data = [
            {
                "id": suggestion.pk,
                "latitude": float(suggestion.latitude),
                "longitude": float(suggestion.longitude),
                "is_new_pin": suggestion.is_new_pin,
                "name": suggestion.suggested_name or (suggestion.pin.effective_name if suggestion.pin else None),
                "hit_count": suggestion.hit_count,
            }
            for suggestion in suggestions
        ]
        return JsonResponse({"suggestions": data})


class PinSuggestionImmichThumbnailView(LoginRequiredMixin, View):
    """GET /memories/locations/<suggestion_id>/immich/thumbnail/<asset_id>/ - proxies one thumbnail.

    Mirrors ``controllers.immich.PinImmichThumbnailView`` (same cache key
    shape, same TTL), but keyed by suggestion instead of pin - a new-pin
    suggestion has no ``Pin`` yet to key off of. Also validates the asset id
    is actually one of this suggestion's ``sample_assets`` - a suggestion is a
    weaker trust boundary than an owned pin, so ownership of the Immich
    account alone isn't treated as enough to fetch an arbitrary asset id.
    """

    def get(self, request: HttpRequest, suggestion_id: int, asset_id: str) -> HttpResponse:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        suggestion = get_object_or_404(PinSuggestion, pk=suggestion_id)
        if suggestion.profile_id != profile.pk:
            raise Http404
        if not any(sample.get("asset_id") == asset_id for sample in suggestion.sample_assets):
            raise Http404

        account = ImmichAccount.objects.get_for_profile(profile)
        if account is None:
            raise Http404
        cache_key = f"ul_immich_thumb_{account.pk}_{asset_id}"
        cached = cache.get(cache_key)
        if cached is not None:
            content, content_type = cached
            return HttpResponse(content, content_type=content_type)

        try:
            content, content_type = ImmichGateway(account=account).get_asset_thumbnail(asset_id)
        except GatewayRequestError:
            return HttpResponse(status=502)
        cache.set(cache_key, (content, content_type), _THUMBNAIL_CACHE_TTL)
        return HttpResponse(content, content_type=content_type)


class PinSuggestionBulkActionView(LoginRequiredMixin, View):
    """Accept or reject many pin suggestions at once.

    POST /memories/locations/bulk/<action>/, JSON body ``{"suggestion_ids": [...]}``.

    Modeled on ``controllers.pin_bulk``'s pattern: non-owned, already-handled,
    or nonexistent ids are silently skipped rather than erroring the whole
    batch. Bulk actions never carry a photo selection - see
    ``services.pin_suggestions.accept_pin_suggestion``; any candidate photos on
    a bulk-accepted suggestion are simply discarded, same as an unchecked
    single accept.
    """

    def post(self, request: HttpRequest, action: str) -> JsonResponse:
        if action not in {"accept", "reject"}:
            raise Http404
        profile, _ = Profile.objects.get_or_create(user=request.user)
        try:
            body = json.loads(request.body)
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
            return JsonResponse({"error": "Invalid request body."}, status=400)

        raw_ids = body.get("suggestion_ids") if isinstance(body, dict) else None
        if not isinstance(raw_ids, list) or not raw_ids:
            return JsonResponse({"error": "No suggestion ids provided."}, status=400)
        if len(raw_ids) > _MAX_BULK_SUGGESTIONS:
            return JsonResponse({"error": f"Too many suggestions at once (max {_MAX_BULK_SUGGESTIONS})."}, status=400)
        try:
            suggestion_ids = [int(raw_id) for raw_id in raw_ids]
        except (TypeError, ValueError):
            return JsonResponse({"error": "Invalid suggestion id."}, status=400)

        suggestions = PinSuggestion.objects.filter(pk__in=suggestion_ids, profile=profile, status=PinSuggestionStatus.PENDING).select_related("pin")
        processed = 0
        for suggestion in suggestions:
            try:
                if action == "reject":
                    reject_pin_suggestion(suggestion)
                else:
                    result = accept_pin_suggestion(suggestion, profile)
                    if result.immich_import_visits:
                        from urbanlens.dashboard.tasks import import_immich_photos

                        safely_enqueue_task(
                            import_immich_photos,
                            result.pin.pk,
                            profile.pk,
                            list(result.immich_import_visits),
                            result.immich_import_visits,
                        )
                processed += 1
            except Exception:
                logger.exception("Bulk pin suggestion action '%s' failed for suggestion %s", action, suggestion.pk)
        return JsonResponse({"ok": True, "processed": processed, "requested": len(suggestion_ids)})


class PinSuggestionActionView(LoginRequiredMixin, View):
    """Accept or reject a single pin suggestion.

    POST /memories/locations/<suggestion_id>/<action>/ where action is "accept" or "reject".
    """

    def _get_suggestion(self, request: HttpRequest, suggestion_id: int) -> tuple[PinSuggestion, Profile]:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        suggestion = get_object_or_404(PinSuggestion.objects.select_related("pin", "pin__location"), pk=suggestion_id)
        if suggestion.profile_id != profile.pk:
            raise Http404
        return suggestion, profile

    def post(self, request: HttpRequest, suggestion_id: int, action: str) -> HttpResponse:
        if action not in {"accept", "reject"}:
            raise Http404
        suggestion, profile = self._get_suggestion(request, suggestion_id)
        if not suggestion.is_actionable:
            return _toast("That suggestion has already been handled.", "info", refresh_queue=True)

        try:
            if action == "reject":
                reject_pin_suggestion(suggestion)
                return _toast("Suggestion dismissed.", "info", refresh_queue=True)

            image_ids = [int(raw_id) for raw_id in request.POST.getlist("image_ids") if raw_id.isdigit()]
            asset_ids = request.POST.getlist("asset_ids")
            result = accept_pin_suggestion(suggestion, profile, image_ids=image_ids, asset_ids=asset_ids)
            if result.immich_import_visits:
                from urbanlens.dashboard.tasks import import_immich_photos

                safely_enqueue_task(
                    import_immich_photos,
                    result.pin.pk,
                    profile.pk,
                    list(result.immich_import_visits),
                    result.immich_import_visits,
                )
            if not result.visits:
                message = f"{'Pin created' if suggestion.is_new_pin else 'Saved'}. Visit logging is turned off, so no visit was recorded."
                return _toast(message, "info", refresh_queue=True)
            plural = "s" if len(result.visits) != 1 else ""
            verb = "Pin created and" if suggestion.is_new_pin else ""
            message = f"{verb} {len(result.visits)} visit{plural} logged for {result.pin.effective_name}.".strip()
            return _toast(message, refresh_queue=True)
        except Exception:
            logger.exception("Pin suggestion action '%s' failed for suggestion %s", action, suggestion_id)
            response = render(request, _CARD_PARTIAL, {"suggestion": suggestion})
            response["HX-Trigger"] = json.dumps({"showToast": {"message": "Something went wrong. Please try again.", "level": "error"}})
            return response
