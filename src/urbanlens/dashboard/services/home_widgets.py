"""The signed-in homepage's customizable widget dashboard.

Defines the fixed catalog of widgets the homepage can show (``HOME_WIDGETS``),
resolves a profile's effective widget layout (enabled widgets, in their
chosen order, plus disabled ones available to re-enable), and builds the data
context each widget's partial template needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from itertools import chain
from typing import TYPE_CHECKING, Any

from django.utils import timezone

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


@dataclass(frozen=True, slots=True)
class HomeWidget:
    """One entry in the homepage's widget catalog."""

    key: str
    label: str
    icon: str
    template: str


#: The full catalog of homepage widgets, in default display order. Adding a
#: new widget here makes it available to every profile automatically (shown,
#: enabled, at the end of the default order) - no backfill needed since
#: ``effective_widget_layout`` treats a profile's saved layout as an ordered
#: subset, not an exhaustive list.
HOME_WIDGETS: tuple[HomeWidget, ...] = (
    HomeWidget("stats", "Your Stats", "bar_chart", "dashboard/partials/home/_widget_stats.html"),
    HomeWidget("safety_checkin", "Active Check-In", "emergency_home", "dashboard/partials/home/_widget_safety_checkin.html"),
    HomeWidget("recent_photos", "Recent Photos", "photo_library", "dashboard/partials/home/_widget_recent_photos.html"),
    HomeWidget("recently_viewed_pins", "Recently Viewed Pins", "history", "dashboard/partials/home/_widget_recently_viewed_pins.html"),
    HomeWidget("recent_pins", "Recently Created Pins", "add_location_alt", "dashboard/partials/home/_widget_recent_pins.html"),
    HomeWidget("priority_pins", "High-Priority Places", "priority_high", "dashboard/partials/home/_widget_priority_pins.html"),
    HomeWidget("recent_comments", "Recent Comments", "forum", "dashboard/partials/home/_widget_recent_comments.html"),
    HomeWidget("recently_viewed_wikis", "Recently Viewed Wikis", "menu_book", "dashboard/partials/home/_widget_recently_viewed_wikis.html"),
    HomeWidget("recent_maps", "Recent Markup Maps", "gesture", "dashboard/partials/home/_widget_recent_maps.html"),
    HomeWidget("recent_trips", "Recent Trip Activity", "luggage", "dashboard/partials/home/_widget_recent_trips.html"),
    HomeWidget("upcoming_trips", "Upcoming Trips", "event_upcoming", "dashboard/partials/home/_widget_upcoming_trips.html"),
)

_WIDGETS_BY_KEY: dict[str, HomeWidget] = {widget.key: widget for widget in HOME_WIDGETS}


def effective_widget_layout(profile: Profile) -> list[dict[str, Any]]:
    """Resolve a profile's homepage widgets: enabled ones (in their chosen order), then disabled ones.

    Args:
        profile: The signed-in profile whose homepage is being rendered/customized.

    Returns:
        A list of ``{"widget": HomeWidget, "enabled": bool}`` covering every
        widget in ``HOME_WIDGETS`` exactly once - enabled widgets first, in
        the profile's saved order, followed by disabled widgets in their
        registry default order.
    """
    saved_keys = [key for key in (profile.home_widget_layout or []) if key in _WIDGETS_BY_KEY]
    if not saved_keys:
        # Never customized (or customized to nothing, which we treat the same
        # way - a homepage with every widget disabled isn't a useful state to
        # persist) - fall back to the full registry, in its default order.
        return [{"widget": widget, "enabled": True} for widget in HOME_WIDGETS]

    ordered_keys = list(dict.fromkeys(saved_keys))  # de-dup, preserve order
    enabled = [_WIDGETS_BY_KEY[key] for key in ordered_keys]
    enabled_set = set(ordered_keys)
    disabled = [widget for widget in HOME_WIDGETS if widget.key not in enabled_set]
    return [{"widget": widget, "enabled": True} for widget in enabled] + [{"widget": widget, "enabled": False} for widget in disabled]


def save_widget_layout(profile: Profile, enabled_keys: list[str]) -> list[str]:
    """Validate and persist a new enabled-widget order for a profile.

    Args:
        profile: The profile customizing their homepage.
        enabled_keys: Widget keys the user wants shown, in their chosen order
            (as submitted by the customize dialog - unrecognized keys and
            duplicates are dropped).

    Returns:
        The validated, de-duplicated key list that was actually saved.
    """
    from urbanlens.dashboard.models.profile.model import Profile

    valid_keys = list(dict.fromkeys(key for key in enabled_keys if key in _WIDGETS_BY_KEY))
    Profile.objects.filter(pk=profile.pk).update(home_widget_layout=valid_keys)
    return valid_keys


def home_dashboard_context(profile: Profile) -> dict[str, Any]:
    """Build the data context every homepage widget partial draws from.

    Args:
        profile: The signed-in user's profile.

    Returns:
        The ``home_*`` context vars consumed by ``partials/home/_widget_*.html``.
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
        "home_stats": [
            {"label": "Maps created", "value": maps_count, "icon": "gesture"},
            {"label": "Wiki edits", "value": WikiEdit.objects.filter(editor=profile).count(), "icon": "edit_note"},
            {"label": "Safety check-ins", "value": safety_checkins_count, "icon": "emergency_home"},
            {"label": "Trips created", "value": trips_created_count, "icon": "add_road"},
            {"label": "Trips joined", "value": trips_joined_count, "icon": "group_add"},
            {"label": "Photos uploaded", "value": photos_count, "icon": "photo_camera"},
            {"label": "Comments posted", "value": comments_count, "icon": "forum"},
            {"label": "Pins rated", "value": Review.objects.filter(profile=profile).count(), "icon": "star"},
        ],
        "home_recent_photos": Image.objects.uploaded_by(profile)[:8],
        "home_recent_pins": Pin.objects.filter(profile=profile).select_related("location").order_by("-created")[:6],
        "home_recent_markup_maps": MarkupMap.objects.for_profile(profile).prefetch_related("items").order_by("-created")[:6],
        "home_priority_unvisited_pins": (
            Pin.objects.filter(profile=profile, priority__gt=0, last_visited__isnull=True, visit_history__isnull=True)
            .select_related("location")
            .order_by("-priority", "-updated")[:6]
        ),
        "home_recent_comments": recent_comments,
        "home_recent_trips": Trip.objects.recently_active_past(profile, since=timezone.now() - timedelta(days=7)),
        "home_upcoming_trips": Trip.objects.upcoming(profile).order_by("start_date", "name")[:6],
        "home_active_checkin": (SafetyCheckin.objects.filter(profile=profile, status__in=active_checkin_statuses).select_related("destination_location").order_by("checkin_by").first()),
    }
