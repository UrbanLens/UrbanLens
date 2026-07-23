"""AI link-extraction views - start a run from the pin page, review past runs.

The review page is deliberately not linked anywhere in the site's navigation
(per the feature request); users reach it through the completion notification.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.views import View

from urbanlens.dashboard.models.link_extraction.model import LinkExtraction
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.ai.link_extraction import (
    LinkExtractionError,
    extractions_remaining_today,
    link_extraction_available,
    start_link_extraction,
)

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = logging.getLogger(__name__)

#: Review page cap - plenty for a per-user history at <=~20 runs/day.
_REVIEW_PAGE_LIMIT = 100


def _toast(message: str, level: str, status: int = 200) -> HttpResponse:
    """A body-less HTMX response that just raises a toast."""
    response = HttpResponse(status=status)
    response["HX-Trigger"] = json.dumps({"showToast": {"level": level, "message": message}})
    return response


class PinLinkExtractionView(LoginRequiredMixin, View):
    """POST: queue an AI extraction run for one of the pin's links."""

    def post(self, request: HttpRequest, pin_slug: str) -> HttpResponse:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        pin = get_object_or_404(Pin, slug=pin_slug, profile=profile)
        try:
            start_link_extraction(request.user, profile, pin, request.POST.get("url", ""))
        except LinkExtractionError as exc:
            return _toast(str(exc), "warning", status=403)
        remaining = extractions_remaining_today(profile)
        return _toast(f"Reading that page in the background - you'll get a notification when it's done. ({remaining} run(s) left today.)", "success")


class AIExtractionReviewView(LoginRequiredMixin, View):
    """GET: the (unlinked) review page listing the user's extraction runs."""

    def get(self, request: HttpRequest) -> HttpResponse:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        return render(
            request,
            "dashboard/pages/ai/extractions.html",
            {
                "page_name": "ai-extractions",
                "extractions": LinkExtraction.objects.for_profile(profile)[:_REVIEW_PAGE_LIMIT],
                "feature_available": link_extraction_available(request.user, profile),
                "remaining_today": extractions_remaining_today(profile),
            },
        )
