# Generic imports
from __future__ import annotations

from datetime import timedelta
from itertools import chain
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views import View
from djangofoundry.controllers import ListController

from urbanlens.dashboard.models.profile import Profile

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse


class IndexController(ListController):
    template_name = "dashboard/pages/home/index.html"
    model = Profile

    def get(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect("home.view")
        return super().get(request, *args, **kwargs)

    @staticmethod
    def page_not_found(request, _exception=None):
        """Project-wide 404 handler - renders the standard error page."""
        return render(request, "dashboard/pages/errors/404.html", status=404)


def own_profile_activity_context(profile: Profile) -> dict[str, object]:
    """Build the private-activity dashboard context for one profile.

    Powers the logged-in homepage's stats grid and recent-activity strips
    (previously the profile page's "My Private Activity" section - moved
    here when the section became the homepage).

    Args:
        profile: The signed-in user's profile.

    Returns:
        The ``profile_*`` context vars consumed by
        ``partials/profile/_private_activity_panel.html``.
    """
    from urbanlens.dashboard.models.comments.model import Comment
    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.models.markup.model import MarkupMap
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.reviews.model import Review
    from urbanlens.dashboard.models.safety.model import SafetyCheckin, SafetyCheckinStatus
    from urbanlens.dashboard.models.trips.model import Trip, TripComment, TripMembership
    from urbanlens.dashboard.models.undo.model import UndoAction
    from urbanlens.dashboard.models.wiki_edit import WikiEdit

    recent_pin_comments = Comment.objects.filter(profile=profile).select_related("pin", "wiki", "wiki__location").order_by("-created")[:5]
    recent_trip_comments = TripComment.objects.by_author(profile)[:5]
    recent_comments = sorted(
        chain(recent_pin_comments, recent_trip_comments),
        key=lambda comment: comment.created,
        reverse=True,
    )[:5]

    active_checkin_statuses = (
        SafetyCheckinStatus.SCHEDULED,
        SafetyCheckinStatus.AWAITING_CHECKIN,
        SafetyCheckinStatus.OVERDUE,
    )

    maps_count = MarkupMap.objects.for_profile(profile).count()
    photos_count = Image.objects.filter(profile=profile).count()
    comments_count = Comment.objects.filter(profile=profile).count() + TripComment.objects.filter(author=profile).count()
    safety_checkins_count = SafetyCheckin.objects.filter(profile=profile).count() + UndoAction.objects.filter(profile=profile, model_label="safety_checkin").count()
    trips_created_count = Trip.objects.filter(creator=profile).count()
    trips_joined_count = TripMembership.objects.filter(profile=profile).exclude(trip__creator=profile).count()

    return {
        "profile_private_stats": [
            {"label": "Maps created", "value": maps_count, "icon": "gesture"},
            {"label": "Wiki edits", "value": WikiEdit.objects.filter(editor=profile).count(), "icon": "edit_note"},
            {"label": "Safety check-ins", "value": safety_checkins_count, "icon": "emergency_home"},
            {"label": "Trips created", "value": trips_created_count, "icon": "add_road"},
            {"label": "Trips joined", "value": trips_joined_count, "icon": "group_add"},
            {"label": "Photos uploaded", "value": photos_count, "icon": "photo_camera"},
            {"label": "Comments posted", "value": comments_count, "icon": "forum"},
            {"label": "Pins rated", "value": Review.objects.filter(profile=profile).count(), "icon": "star"},
        ],
        "profile_recent_photos": Image.objects.uploaded_by(profile)[:8],
        "profile_recent_pins": Pin.objects.filter(profile=profile).select_related("location").order_by("-created")[:6],
        "profile_recent_markup_maps": MarkupMap.objects.for_profile(profile).prefetch_related("items").order_by("-created")[:6],
        "profile_priority_unvisited_pins": (
            Pin.objects.filter(profile=profile, priority__gt=0, last_visited__isnull=True, visit_history__isnull=True)
            .select_related("location")
            .order_by("-priority", "-updated")[:6]
        ),
        "profile_recent_comments": recent_comments,
        "profile_recent_trips": Trip.objects.recently_active_past(profile, since=timezone.now() - timedelta(days=7)),
        "profile_upcoming_trips": Trip.objects.upcoming(profile).order_by("start_date", "name")[:6],
        "profile_active_checkin": (SafetyCheckin.objects.filter(profile=profile, status__in=active_checkin_statuses).select_related("destination_location").order_by("checkin_by").first()),
    }


class HomeOverviewView(LoginRequiredMixin, View):
    """The logged-in homepage: a private dashboard overview.

    GET /dashboard/home/
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        """Render the private activity dashboard for the signed-in user.

        Args:
            request: The authenticated request.

        Returns:
            The rendered homepage.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        display_name = profile.first_name or profile.username
        context: dict[str, object] = {
            "profile": profile,
            "page_name": "home-overview",
            "hero_title": f"Welcome back, {display_name}",
            **own_profile_activity_context(profile),
        }
        return render(request, "dashboard/pages/home/overview.html", context)
