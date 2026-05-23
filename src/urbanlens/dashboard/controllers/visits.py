"""Visit history controller - HTMX views for PinVisit CRUD on the pin detail page."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.views import View

from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource

logger = logging.getLogger(__name__)


def _sync_last_visited(pin: Pin) -> None:
    """Recompute pin.last_visited from the most recent PinVisit row.

    Args:
        pin: Pin instance to update in-place (saves only last_visited field).
    """
    latest = (
        pin.visit_history.order_by("-visited_at")
        .values_list("visited_at", flat=True)
        .first()
    )
    pin.last_visited = latest
    pin.save(update_fields=["last_visited"])


class VisitHistoryView(LoginRequiredMixin, View):
    """List existing visits and add new manual visit entries (HTMX partial).

    GET  /map/pin/<pin_id>/visits/  → renders the full visit history panel
    POST /map/pin/<pin_id>/visits/  → creates a new visit, returns updated panel
    """

    def get(self, request: HttpRequest, pin_id: int) -> HttpResponse:
        """Render the visit history panel for a pin.

        Args:
            request: Incoming HTTP request.
            pin_id: Primary key of the target pin.

        Returns:
            Rendered HTML partial.
        """
        pin = get_object_or_404(Pin, id=pin_id, profile__user=request.user)
        return render(
            request,
            "dashboard/partials/_visit_history.html",
            {"pin": pin, "visits": pin.visit_history.all()},
        )

    def post(self, request: HttpRequest, pin_id: int) -> HttpResponse:
        """Create a new manual visit entry and return the updated panel.

        Args:
            request: Incoming HTTP request. POST body must include ``visited_at``
                (datetime-local string) and optionally ``notes``.
            pin_id: Primary key of the target pin.

        Returns:
            Rendered HTML partial, or 400 on validation failure.
        """
        pin = get_object_or_404(Pin, id=pin_id, profile__user=request.user)

        raw_date = request.POST.get("visited_at", "").strip()
        if not raw_date:
            return HttpResponse("Date is required.", status=400)
        try:
            visited_at = datetime.fromisoformat(raw_date)
            if visited_at.tzinfo is None:
                visited_at = visited_at.replace(tzinfo=timezone.utc)
        except ValueError:
            return HttpResponse("Invalid date format.", status=400)

        notes = request.POST.get("notes", "").strip() or None
        PinVisit.objects.create(pin=pin, visited_at=visited_at, notes=notes, source=VisitSource.MANUAL)
        _sync_last_visited(pin)

        return render(
            request,
            "dashboard/partials/_visit_history.html",
            {"pin": pin, "visits": pin.visit_history.all()},
        )


class VisitDeleteView(LoginRequiredMixin, View):
    """Delete a single PinVisit and return the refreshed panel (HTMX).

    POST /map/pin/<pin_id>/visits/<visit_id>/delete/
    """

    def post(self, request: HttpRequest, pin_id: int, visit_id: int) -> HttpResponse:
        """Delete the specified visit and return the updated panel.

        Args:
            request: Incoming HTTP request.
            pin_id: Primary key of the pin.
            visit_id: Primary key of the visit to delete.

        Returns:
            Rendered HTML partial.
        """
        visit = get_object_or_404(
            PinVisit,
            id=visit_id,
            pin__id=pin_id,
            pin__profile__user=request.user,
        )
        pin = visit.pin
        visit.delete()
        _sync_last_visited(pin)

        return render(
            request,
            "dashboard/partials/_visit_history.html",
            {"pin": pin, "visits": pin.visit_history.all()},
        )
