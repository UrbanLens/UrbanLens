"""Visit history controller - HTMX views for PinVisit CRUD on the pin detail page."""

from __future__ import annotations

from datetime import UTC, datetime
import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.views import View

from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.visit_suggestions.model import VisitSuggestion
from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource
from urbanlens.dashboard.services.connections import get_connections
from urbanlens.dashboard.services.map_snapshot import parse_map_data
from urbanlens.dashboard.services.pagination import get_page
from urbanlens.dashboard.services.visits import (
    add_visited_status,
    create_visit_suggestion,
    sync_last_visited,
)

logger = logging.getLogger(__name__)

_VISITS_PAGE_SIZE = 6


def _visit_dialog_context(pin: Pin) -> dict[str, object]:
    """Build the shared context the add/edit visit dialogs need.

    Args:
        pin: Pin the dialog operates on.

    Returns:
        Context dict with ``pin``, the owner's ``pin_images`` (offered in the
        existing-photo picker), and taggable ``connections``.
    """
    return {
        "pin": pin,
        # The pin owner's own photos already on this pin, offered in the visit
        # dialog so they can attach existing gallery photos to the visit.
        "pin_images": list(
            Image.objects.filter(pin=pin, profile=pin.profile).order_by("-created")[:60],
        ),
        "connections": get_connections(pin.profile),
    }


def _render_visit_history(request: HttpRequest, pin: Pin) -> HttpResponse:
    """Render the visit history panel for a pin, paginated newest-first.

    Args:
        request: Incoming HTTP request (read for an optional ``page`` param).
        pin: Pin whose visit history should be rendered.

    Returns:
        Rendered HTML partial.
    """
    page_obj = get_page(request, pin.visit_history.all().prefetch_related("participants", "images"), _VISITS_PAGE_SIZE)
    pending_suggestions = (
        VisitSuggestion.objects.for_profile(pin.profile)
        .pending()
        .for_place(location=pin.location, latitude=pin.effective_latitude, longitude=pin.effective_longitude)
        .select_related("suggested_by", "existing_visit")
        .prefetch_related("candidate_profiles")
        .order_by("-created")
    )
    return render(
        request,
        "dashboard/partials/pins/_visit_history.html",
        {
            **_visit_dialog_context(pin),
            "page_obj": page_obj,
            "visits": page_obj.object_list,
            "pending_suggestions": pending_suggestions,
        },
    )


def _sync_visit_photos(request: HttpRequest, pin: Pin, visit: PinVisit) -> bool:
    """Reconcile the photos attached to a visit from the submitted form.

    Handles both the create and edit flows:

    - New files (POST ``photos``) are created as ``Image`` rows tied to the pin,
      its location, the owner, and this visit, then queued for EXIF processing.
    - Selected existing photos (POST ``existing_photo_ids``) - already in the pin
      gallery - have their ``visit`` FK pointed at this visit.
    - Any gallery photo previously attached to this visit but no longer selected
      is detached (its ``visit`` FK is cleared). Freshly uploaded photos are
      never detached. Only the owner's own photos are ever touched.

    Args:
        request: Incoming request carrying the files and selected ids.
        pin: The pin the visit belongs to.
        visit: The visit to reconcile photos for.

    Returns:
        True if any brand-new file was uploaded (so callers can refresh the
        gallery), False otherwise.
    """
    from urbanlens.dashboard.services.celery import safely_enqueue_task
    from urbanlens.dashboard.tasks import process_image_upload

    uploaded_pks: list[int] = []
    for image_file in request.FILES.getlist("photos"):
        img = Image.objects.create(
            image=image_file,
            pin=pin,
            location=pin.location,
            profile=pin.profile,
            visit=visit,
        )
        safely_enqueue_task(process_image_upload, img.pk)
        uploaded_pks.append(img.pk)

    selected_ids = {int(pid) for pid in request.POST.getlist("existing_photo_ids") if pid.strip().isdigit()}
    owner_gallery = Image.objects.filter(pin=pin, profile=pin.profile)
    if selected_ids:
        owner_gallery.filter(pk__in=selected_ids).update(visit=visit)
    # Detach gallery photos that were on this visit but are no longer selected.
    keep = selected_ids | set(uploaded_pks)
    owner_gallery.filter(visit=visit).exclude(pk__in=keep).update(visit=None)

    return bool(uploaded_pks)


def _parse_visited_at(request: HttpRequest) -> datetime | None:
    """Build a timezone-aware ``visited_at`` from the POST date/time fields.

    Args:
        request: Request carrying ``visited_date`` (required) and optional
            ``visited_time``.

    Returns:
        The parsed datetime, or None if the date is missing or malformed.
    """
    raw_date = request.POST.get("visited_date", "").strip()
    raw_time = request.POST.get("visited_time", "").strip()
    if not raw_date:
        return None
    try:
        iso_str = f"{raw_date}T{raw_time}" if raw_time else f"{raw_date}T00:00"
        return datetime.fromisoformat(iso_str).replace(tzinfo=UTC)
    except ValueError:
        return None


def _resolve_participants(request: HttpRequest, pin: Pin) -> list:
    """Resolve the submitted participant ids to the owner's actual connections.

    Args:
        request: Request carrying ``participant_ids``.
        pin: Pin whose owner's connections bound the allowed participants.

    Returns:
        List of Profile instances the owner is connected to.
    """
    connections_by_id = {p.pk: p for p in get_connections(pin.profile)}
    participant_ids = {int(pid) for pid in request.POST.getlist("participant_ids") if pid.strip().isdigit()}
    return [connections_by_id[pid] for pid in participant_ids if pid in connections_by_id]


class VisitHistoryView(LoginRequiredMixin, View):
    """List existing visits and add new manual visit entries (HTMX partial).

    GET  /map/pin/<pin_id>/visits/  → renders the full visit history panel
    POST /map/pin/<pin_id>/visits/  → creates a new visit, returns updated panel
    """

    def get(self, request: HttpRequest, pin_slug) -> HttpResponse:
        """Render the visit history panel for a pin.

        Args:
            request: Incoming HTTP request.
            pin_slug: Primary key of the target pin.

        Returns:
            Rendered HTML partial.
        """
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        return _render_visit_history(request, pin)

    def post(self, request: HttpRequest, pin_slug) -> HttpResponse:
        """Create a new manual visit entry and return the updated panel.

        Args:
            request: Incoming HTTP request. POST body must include
                ``visited_date`` and optionally ``visited_time``, ``notes``,
                ``participant_ids``, ``map_data`` (a Leaflet snapshot),
                ``photos`` (newly uploaded files), and ``existing_photo_ids``
                (ids of gallery photos to link to this visit).
            pin_slug: Primary key of the target pin.

        Returns:
            Rendered HTML partial, or 400 on validation failure.
        """
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)

        visited_at = _parse_visited_at(request)
        if visited_at is None:
            return HttpResponse("A valid date is required.", status=400)

        notes = request.POST.get("notes", "").strip() or None
        map_data = parse_map_data(request)
        visit = PinVisit.objects.create(
            pin=pin,
            visited_at=visited_at,
            notes=notes,
            source=VisitSource.MANUAL,
            map_data=map_data,
        )
        sync_last_visited(pin)
        add_visited_status(pin)

        uploaded_new = _sync_visit_photos(request, pin, visit)

        participants = _resolve_participants(request, pin)
        if participants:
            visit.participants.set(participants)

        lat, lng = pin.effective_latitude, pin.effective_longitude
        if participants and lat is not None and lng is not None:
            for participant in participants:
                others = [p for p in participants if p.pk != participant.pk]
                create_visit_suggestion(
                    suggested_to=participant,
                    suggested_by=pin.profile,
                    visited_at=visited_at,
                    location=pin.location,
                    latitude=lat,
                    longitude=lng,
                    candidate_profiles=others,
                    origin_visit=visit,
                    origin_pin=pin,
                )

        response = _render_visit_history(request, pin)
        if uploaded_new:
            # Tell the photo gallery panel to reload so freshly uploaded photos
            # appear there too (see the refreshGallery listener in _photo_gallery.html).
            response["HX-Trigger"] = "refreshGallery"
        return response


class VisitEditView(LoginRequiredMixin, View):
    """Edit an existing PinVisit (HTMX partial).

    GET  /map/pin/<pin_slug>/visits/<visit_id>/edit/  → renders the edit form
    POST /map/pin/<pin_slug>/visits/<visit_id>/edit/  → saves and returns the panel
    """

    def get(self, request: HttpRequest, pin_slug, visit_id: int) -> HttpResponse:
        """Render the pre-filled edit form for a single visit.

        Args:
            request: Incoming HTTP request.
            pin_slug: Slug of the pin the visit belongs to.
            visit_id: Primary key of the visit to edit.

        Returns:
            Rendered edit-form HTML partial.
        """
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        visit = get_object_or_404(
            PinVisit.objects.prefetch_related("participants", "images"),
            id=visit_id,
            pin=pin,
        )
        return render(
            request,
            "dashboard/partials/pins/_visit_form.html",
            {
                **_visit_dialog_context(pin),
                "visit": visit,
                "dialog_id": f"visit-edit-dialog-{pin.slug}",
            },
        )

    def post(self, request: HttpRequest, pin_slug, visit_id: int) -> HttpResponse:
        """Apply edits to an existing visit and return the updated panel.

        Args:
            request: Incoming HTTP request carrying the same fields as the add
                form (``visited_date``, ``visited_time``, ``notes``, ``map_data``,
                ``photos``, ``existing_photo_ids``, ``participant_ids``).
            pin_slug: Slug of the pin the visit belongs to.
            visit_id: Primary key of the visit to edit.

        Returns:
            Rendered HTML partial, or 400 on validation failure.
        """
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        visit = get_object_or_404(PinVisit, id=visit_id, pin=pin)

        visited_at = _parse_visited_at(request)
        if visited_at is None:
            return HttpResponse("A valid date is required.", status=400)

        visit.visited_at = visited_at
        visit.notes = request.POST.get("notes", "").strip() or None
        visit.map_data = parse_map_data(request)
        visit.save()
        sync_last_visited(pin)

        uploaded_new = _sync_visit_photos(request, pin, visit)
        visit.participants.set(_resolve_participants(request, pin))

        response = _render_visit_history(request, pin)
        if uploaded_new:
            response["HX-Trigger"] = "refreshGallery"
        return response


class VisitDeleteView(LoginRequiredMixin, View):
    """Delete a single PinVisit and return the refreshed panel (HTMX).

    POST /map/pin/<pin_id>/visits/<visit_id>/delete/
    """

    def post(self, request: HttpRequest, pin_slug, visit_id: int) -> HttpResponse:
        """Delete the specified visit and return the updated panel.

        Args:
            request: Incoming HTTP request.
            pin_slug: Primary key of the pin.
            visit_id: Primary key of the visit to delete.

        Returns:
            Rendered HTML partial.
        """
        visit = get_object_or_404(
            PinVisit,
            id=visit_id,
            pin__slug=pin_slug,
            pin__profile__user=request.user,
        )
        pin = visit.pin
        visit.delete()
        sync_last_visited(pin)

        return _render_visit_history(request, pin)
