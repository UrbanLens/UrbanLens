"""The one user action in the shared-place flow: accept or decline bringing your own notes."""

from __future__ import annotations

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404
from django.views import View

from urbanlens.dashboard.models.place.shared import NotesOffer
from urbanlens.dashboard.services.places.shared_places import answer_notes_offer


class NotesOfferAnswerView(LoginRequiredMixin, View):
    """POST: answer your own notes offer. Anyone else's offer, or none, is the same 404."""

    def post(self, request: HttpRequest, offer_id: int, action: str) -> HttpResponse:
        if action not in {"accept", "decline"}:
            raise Http404
        offer = get_object_or_404(NotesOffer, pk=offer_id, profile__user=request.user)
        if not answer_notes_offer(offer, offer.profile, accept=action == "accept"):
            raise Http404
        return HttpResponse(status=204)
