"""Memories → Locations page: review queue for PinImportFailure rows.

Failures are raised by ``tasks.resolve_deferred_pin_locations`` (via
``services.pins.pin_import_failures.record_pin_import_failure``) when a pin's Google Maps
CID can't be resolved to a location - Google has no data for the place, or the live
lookup itself stalled or errored out. This controller only lets the owner act on what
was already recorded: supply an address or coordinates to place the pin themselves, or
dismiss the entry. It never records a failure itself.
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

from urbanlens.dashboard.models.pin_import_failures.model import PinImportFailure
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.pins.pin_creation import PinCreationError
from urbanlens.dashboard.services.pins.pin_import_failures import dismiss_pin_import_failure, resolve_pin_import_failure

if TYPE_CHECKING:
    from django.db.models import QuerySet
    from django.http import HttpRequest

logger = logging.getLogger(__name__)

_QUEUE_PARTIAL = "dashboard/partials/memories/_pin_import_failures_queue.html"
_CARD_PARTIAL = "dashboard/partials/memories/_pin_import_failure_card.html"
_GUESS_PARTIAL = "dashboard/partials/memories/_pin_import_failure_guess.html"


def pending_pin_import_failures(profile: Profile) -> QuerySet[PinImportFailure]:
    """The profile's pending import failures, newest first.

    Args:
        profile: Owner whose pending failures to fetch.

    Returns:
        Newest-first queryset of pending rows.
    """
    return PinImportFailure.objects.for_profile(profile).pending().order_by("-created")


def _toast(message: str, level: str = "success", *, status: int = 200, refresh_queue: bool = False, view_pin_url: str | None = None) -> HttpResponse:
    """Return an empty HTMX response that removes the swapped card and fires a toast.

    Mirrors ``controllers.pin_merge_suggestions._toast``.

    Args:
        message: Toast body text (HTML-escaped by the caller if it embeds
            any dynamic value).
        level: toastr level ("success", "info", "warning", "error").
        status: HTTP status code for the (otherwise empty) response.
        refresh_queue: Whether to also fire the ``refreshQueue`` htmx event.
        view_pin_url: If set, appends a "View pin" link to the toast.

    Returns:
        An empty 200 (or ``status``) response carrying the toast/refresh
        triggers in its ``HX-Trigger`` header.
    """
    if view_pin_url:
        message += f' <a href="{view_pin_url}" class="toast-undo-btn">View pin</a>'
    triggers: dict[str, Any] = {"showToast": {"message": message, "level": level}}
    if refresh_queue:
        triggers["refreshQueue"] = True
    response = HttpResponse("", status=status)
    response["HX-Trigger"] = json.dumps(triggers)
    return response


def _get_failure(request: HttpRequest, failure_id: int) -> tuple[PinImportFailure, Profile]:
    """Fetch a failure owned by the current profile, or raise 404.

    Args:
        request: The current request (used to resolve the owning profile).
        failure_id: Primary key of the ``PinImportFailure`` being acted on.

    Returns:
        Tuple of the failure and the resolved profile.

    Raises:
        Http404: No such failure exists, or it belongs to a different profile.
    """
    profile, _ = Profile.objects.get_or_create(user=request.user)
    failure = get_object_or_404(PinImportFailure, pk=failure_id)
    if failure.profile_id != profile.pk:
        raise Http404
    return failure, profile


def _parse_optional_float(raw: str) -> float | None:
    """Parse a POST field as a float, treating a blank string as unset.

    Args:
        raw: Raw field value from the request.

    Returns:
        The parsed float, or None if ``raw`` was blank.

    Raises:
        ValueError: ``raw`` was non-blank but not a valid float.
    """
    stripped = raw.strip()
    return float(stripped) if stripped else None


class PinImportFailureQueuePartialView(LoginRequiredMixin, View):
    """Just the import-failure queue partial, re-fetched via the ``refreshQueue`` event.

    GET /memories/locations/import-failures/queue/

    Unpaginated, like ``pin_merge_suggestions`` - these are expected to be
    rare (one per cid Google genuinely couldn't place), not a routine volume
    like photo-scan suggestions.
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        """Render the current queue of pending import failures for this profile.

        Args:
            request: The incoming GET request.

        Returns:
            The rendered queue partial.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        return render(request, _QUEUE_PARTIAL, {"pin_import_failures": list(pending_pin_import_failures(profile))})


class PinImportFailureGuessView(LoginRequiredMixin, View):
    """Suggest a location for one import failure, from its name.

    GET /memories/locations/import-failures/<failure_id>/guess/

    Fetched per card on reveal rather than computed while rendering the queue:
    a single import can leave hundreds of failures, and each guess costs a
    geocoder call. Building them all up front would make the page wait on
    hundreds of sequential lookups, and would spend that quota on cards the user
    never scrolls to.

    Answers with an empty body when there is no confident guess, which is the
    normal case for a vague name - the card then shows nothing extra.
    """

    def get(self, request: HttpRequest, failure_id: int) -> HttpResponse:
        """Render the suggestion for this failure, if there is one.

        Args:
            request: The incoming GET request.
            failure_id: PK of the failure to guess a location for.

        Returns:
            The rendered suggestion partial, or an empty response.
        """
        from urbanlens.dashboard.services.pins.import_failure_guess import guess_for_failure

        profile, _ = Profile.objects.get_or_create(user=request.user)
        failure = get_object_or_404(PinImportFailure, pk=failure_id, profile=profile)
        if not failure.is_actionable:
            return HttpResponse("")

        guess = guess_for_failure(failure)
        if guess is None:
            return HttpResponse("")
        return render(request, _GUESS_PARTIAL, {"failure": failure, "guess": guess})


class PinImportFailureResolveView(LoginRequiredMixin, View):
    """Manually place a pin for a single import failure.

    POST /memories/locations/import-failures/<failure_id>/resolve/

    Body: ``address`` (free text, geocoded) or ``latitude``/``longitude``
    (marker coordinates). Invalid coordinates or a ``PinCreationError`` from
    the underlying placement re-render the card in place with an error toast,
    rather than failing with a 500.
    """

    def post(self, request: HttpRequest, failure_id: int) -> HttpResponse:
        """Place a pin for this failure from the submitted address or coordinates.

        Args:
            request: The incoming POST request, carrying ``address`` and/or
                ``latitude``/``longitude`` form fields.
            failure_id: Primary key of the ``PinImportFailure`` being resolved.

        Returns:
            An empty toast response on success (or an already-handled
            no-op), or a re-rendered card with an error toast when the
            input or placement was invalid.
        """
        failure, profile = _get_failure(request, failure_id)
        if not failure.is_actionable:
            return _toast("That entry has already been handled.", "info", refresh_queue=True)

        address = request.POST.get("address", "").strip() or None
        try:
            latitude = _parse_optional_float(request.POST.get("latitude", ""))
            longitude = _parse_optional_float(request.POST.get("longitude", ""))
        except ValueError:
            response = render(request, _CARD_PARTIAL, {"failure": failure})
            response["HX-Trigger"] = json.dumps({"showToast": {"message": "Enter a valid latitude and longitude.", "level": "error"}})
            return response

        try:
            pin = resolve_pin_import_failure(failure, profile, address=address, latitude=latitude, longitude=longitude)
        except PinCreationError as exc:
            response = render(request, _CARD_PARTIAL, {"failure": failure})
            response["HX-Trigger"] = json.dumps({"showToast": {"message": exc.safe_message, "level": "error"}})
            return response

        view_pin_url = reverse("pin.details", args=[pin.slug or pin.uuid])
        return _toast(f"Pin placed for {pin.effective_name}.", refresh_queue=True, view_pin_url=view_pin_url)


class PinImportFailureDismissView(LoginRequiredMixin, View):
    """Dismiss a single import failure without placing a pin for it.

    POST /memories/locations/import-failures/<failure_id>/dismiss/
    """

    def post(self, request: HttpRequest, failure_id: int) -> HttpResponse:
        """Dismiss this failure without placing a pin for it.

        Args:
            request: The incoming POST request.
            failure_id: Primary key of the ``PinImportFailure`` being dismissed.

        Returns:
            An empty toast response.
        """
        failure, _profile = _get_failure(request, failure_id)
        if not failure.is_actionable:
            return _toast("That entry has already been handled.", "info", refresh_queue=True)

        dismiss_pin_import_failure(failure)
        return _toast("Removed from the list.", "info", refresh_queue=True)
