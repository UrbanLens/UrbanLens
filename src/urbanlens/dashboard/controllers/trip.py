"""Trip planning controllers."""

from __future__ import annotations

import datetime
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.html import escape
from django.views import View
import requests

from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trips.model import (
    Trip,
    TripActivity,
    TripMembership,
)
from urbanlens.dashboard.services.core.counters import Outage
from urbanlens.dashboard.services.core.gateway import GatewayRequestError
from urbanlens.dashboard.services.security.throttle import Rate
from urbanlens.dashboard.services.trips.trip_access import (
    can_perform as _can_perform,
    get_trip_for_viewer,
    has_joined as _viewer_has_joined,
    is_organizer as _is_organizer,
)
from urbanlens.dashboard.services.trips.trip_activities import (
    activity_queryset as _activity_qs,
    build_activity_rows,
    complete_activity,
    create_activity,
    delete_activity,
    move_activity,
    parse_scheduled_at as _parse_scheduled_at,
    reorder_activities,
    set_activity_position,
    set_activity_rsvp,
    set_activity_status,
    set_activity_vote,
    update_activity,
)
from urbanlens.dashboard.services.trips.trip_comments import ALLOWED_COMMENT_EMOJIS, TripCommentData, add_comment, build_comment_tree, delete_comment, get_comment
from urbanlens.dashboard.services.trips.trip_crud import TRIP_DELETED_MESSAGE, create_trip, delete_trip, set_trip_permissions, update_trip
from urbanlens.dashboard.services.trips.trip_errors import TripError, TripMemberNotFoundError, TripNotFoundError, TripPermissionError, TripRateLimitError
from urbanlens.dashboard.services.trips.trip_invitations import invitations_visible_to, invite_to_trip_by_email, is_valid_address, parse_address_list
from urbanlens.dashboard.services.trips.trip_legs import activity_coords
from urbanlens.dashboard.services.trips.trip_map import build_trip_map_points
from urbanlens.dashboard.services.trips.trip_membership import (
    add_member_by_username,
    addable_friends as _addable_friends,
    join_trip,
    leave_trip,
    list_members,
    remove_member,
    require_trip_creator,
    resolve_trip_member,
    set_member_organizer,
    set_trip_rsvp,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from django.http import HttpRequest

    from urbanlens.dashboard.services.apis.weather.forecast import ForecastSlot

logger = logging.getLogger(__name__)

#: HTTP status each shared trip-service error maps to on the internal surface. The external API keeps its own
#: copy of this mapping (it answers with JSON rather than the plain-text bodies HTMX turns into toasts), but
#: both derive the status from the same exception classes.
_TRIP_ERROR_STATUS: dict[type[TripError], int] = {
    TripNotFoundError: 404,
    TripPermissionError: 403,
    TripRateLimitError: 429,
}


def _trip_error_response(exc: TripError) -> HttpResponse:
    """Turn a shared trip-service error into the plain-text response HTMX expects.

    Bodies stay plain text (not JSON) because the site's global ``htmx:responseError`` handler surfaces
    the raw body as a toast.

    Args:
        exc: The error raised by a trip service.

    Returns:
        A response carrying the error message and its mapped status; anything not specifically mapped is
        a 400.
    """
    # The service carries the raw username so each surface escapes for its own
    # medium - HTML here, unescaped JSON in the external API.
    message = f'No user found with username "{escape(exc.username)}".' if isinstance(exc, TripMemberNotFoundError) else exc.message
    status = next((code for cls, code in _TRIP_ERROR_STATUS.items() if isinstance(exc, cls)), 400)
    return HttpResponse(message, status=status)


#: Valid `sort`/`dir` query params for the trips list page (see `TripListView`/`TripCreateView`).
TRIP_LIST_SORT_CHOICES = ("start_date", "updated")
TRIP_LIST_DIRECTION_CHOICES = ("asc", "desc")


#: Trip cards per page on the trips list.
TRIP_LIST_PAGE_SIZE = 24


def trip_list_context(request: HttpRequest, profile: Profile) -> dict[str, Any]:
    """One page of the viewer's trips for ``trip_list_partial.html``, masked and sorted per the request.

    Args:
        request: Carries ``sort``/``dir`` and ``page``.
        profile: The viewer.

    Returns:
        Context with ``trips`` (the page), ``page_obj`` and the sort state.
    """
    from urllib.parse import urlencode

    from django.urls import reverse

    from urbanlens.dashboard.services.core.pagination import get_page

    sort, direction = _trip_list_sort_params(request)
    page = get_page(request, Trip.objects.for_list_page(profile, sort=sort, direction=direction), TRIP_LIST_PAGE_SIZE)
    trips = list(page.object_list)
    _apply_trip_list_identity_masking(profile, trips)
    _annotate_viewer_membership(profile, trips)
    return {
        "trips": trips,
        "page_obj": page,
        "page_url": reverse("trips.list"),
        "extra_query": urlencode({"sort": sort, "dir": direction}),
        "profile": profile,
        "sort": sort,
        "dir": direction,
    }


def _apply_trip_list_identity_masking(viewer: Profile, trips: Iterable[Trip]) -> None:
    """Mask each listed trip's member/creator identities the viewer may not see.

    Mutates each ``TripMembership.profile``/``Trip.creator`` object in place (see
    ``identity_visibility.mask_profile_references``) so ``trip_list_partial.html`` can render
    ``display_name``/``display_avatar_url`` instead of the raw username/avatar.

    Args:
        viewer: The profile viewing the list.
        trips: Trips about to be rendered via ``trip_list_partial.html``.
    """
    from urbanlens.dashboard.services.profile.identity_visibility import mask_profile_references

    all_refs: list[Profile] = []
    for trip in trips:
        if trip.creator is not None:
            all_refs.append(trip.creator)
        all_refs.extend(membership.profile for membership in trip.memberships.all())

    mask_profile_references(viewer, all_refs)


def _annotate_viewer_membership(viewer: Profile, trips: Iterable[Trip]) -> None:
    """Attach ``trip.viewer_membership`` (or ``None``) to each listed trip.

    Reuses the ``memberships`` prefetch ``for_list_page`` already loads, so ``trip_list_partial.html``
    can gate viewer-only actions (e.g. "Start a check-in") on join status without an extra query per
    card.

    Args:
        viewer: The profile viewing the list.
        trips: Trips about to be rendered via ``trip_list_partial.html``.
    """
    for trip in trips:
        trip.viewer_membership = next((m for m in trip.memberships.all() if m.profile_id == viewer.id), None)


def _trip_list_sort_params(request: HttpRequest) -> tuple[str, str]:
    """Read and validate the `sort`/`dir` query params for the trips list page.

    Args:
        request: The incoming request.

    Returns:
        A ``(sort, direction)`` tuple, each guaranteed to be one of the valid choices.
    """
    sort = request.GET.get("sort", "updated")
    if sort not in TRIP_LIST_SORT_CHOICES:
        sort = "updated"
    direction = request.GET.get("dir", "desc")
    if direction not in TRIP_LIST_DIRECTION_CHOICES:
        direction = "desc"
    return sort, direction


def trip_or_not_found(request: HttpRequest, trip_slug: str, profile: Profile) -> Trip | HttpResponse:
    """Return the trip when *profile* may see it, else the styled "not found" page.

    That version rendered the same page for a missing trip and for one the viewer had no access to, but
    answered 404 for the first and 403 for the second - so the status code alone distinguished "no such
    slug" from "somebody else's trip", which is exactly the enumeration the shared page was meant to
    prevent.

    Args:
        request: The incoming request (needed to render the page).
        trip_slug: The trip's URL slug.
        profile: The viewing profile.

    Returns:
        The trip, or a 404 response rendering the "not found" page.
    """
    try:
        return get_trip_for_viewer(trip_slug, profile)
    except TripNotFoundError:
        return render(request, "dashboard/pages/trips/not_found.html", status=404)


def _render_members_panel(request: HttpRequest, trip: Trip, profile: Profile) -> HttpResponse:
    """Re-render the members panel partial.

    Args:
        request: The incoming request.
        trip: The trip whose roster is being rendered.
        profile: The viewing profile.

    Returns:
        The rendered members panel.
    """
    return render(
        request,
        "dashboard/partials/trips/trip_members_panel.html",
        {
            "trip": trip,
            "members": list_members(trip, profile),
            "profile": profile,
            "addable_friends": _addable_friends(trip, profile),
            "can_add_members": _can_perform(profile, trip, trip.allow_add_members),
            "email_invitations": invitations_visible_to(trip, profile),
        },
    )


def _activities_panel_html(request: HttpRequest, trip: Trip, profile: Profile, *, oob: bool = False) -> str:
    """Render just the activities panel markup (index map, vote counts, per-activity permissions).

    Split out from ``_render_activities_panel`` so other views whose primary response is a different
    panel (e.g. toggling a member's organizer status) can still include a fresh copy as an out-of-band
    swap - organizer status feeds directly into each activity's ``can_manage`` flag here.

    Args:
        request: The incoming request.
        trip: The trip whose itinerary is being rendered.
        profile: The viewing profile.
        oob: When True, marks the rendered root element ``hx-swap-oob="true"`` so it can be concatenated
        onto another view's primary response instead...

    Returns:
        The rendered activities-panel markup.
    """
    activities_with_index = build_activity_rows(trip, profile)
    activities = [row["activity"] for row in activities_with_index]
    viewer_has_joined = _viewer_has_joined(profile, trip)

    all_activities_completed = bool(activities) and all(act.status == TripActivity.STATUS_COMPLETED for act in activities)
    # Empty-tab greying - the Upcoming tab always has something to say (even
    # "no upcoming activities"), so only these 3 status-filtered tabs need it.
    proposed_count = sum(1 for act in activities if act.status == TripActivity.STATUS_PROPOSED)
    confirmed_count = sum(1 for act in activities if act.status == TripActivity.STATUS_CONFIRMED)
    completed_count = sum(1 for act in activities if act.status == TripActivity.STATUS_COMPLETED)
    return render_to_string(
        request=request,
        template_name="dashboard/partials/trips/trip_activities_panel.html",
        context={
            "trip": trip,
            "activities_with_index": activities_with_index,
            "profile": profile,
            "all_activities_completed": all_activities_completed,
            "proposed_count": proposed_count,
            "confirmed_count": confirmed_count,
            "completed_count": completed_count,
            "viewer_has_joined": viewer_has_joined,
            "oob": oob,
        },
    )


def _trip_hero_oob(request: HttpRequest, trip: Trip) -> str:
    """Render the page hero as an out-of-band HTMX swap.

    The hero lives in base.html's ``{% block hero %}`` (outside ``#trip-header``, as a sibling of ``{%
    block subnav %}``) so it renders in the correct spot above the page container, but its
    name/description/date-range display still needs to stay in sync after an edit or an activity date
    change - see ``TripEditView`` and ``_render_activities_panel``.
    """
    from django.urls import reverse

    return render_to_string(
        request=request,
        template_name="dashboard/partials/ui/_page_hero.html",
        context={
            "trip": trip,
            "id": "trip-hero",
            "oob": True,
            "body_template": "dashboard/partials/trips/_trip_detail_hero_body.html",
            "back_url": reverse("trips.overview"),
            "back_label": "Plan",
            "modifier": "top",
        },
    )


def _render_activities_panel(request: HttpRequest, trip: Trip, profile: Profile) -> HttpResponse:
    """Re-render the activities panel as the primary HTMX response.

    Bundles out-of-band refreshes so sibling elements don't go stale after an activity
    add/edit/delete/complete:

    - ``#trip-header``/``#trip-hero``: an activity add/edit/delete/complete can change the trip's
      persisted date range (see ``_expand_trip_dates``) - keep the head...
    - the weather panel can't be refreshed the same cheap way (it's a live external API call), so it's
      told to re-fetch itself via HX-Trigger, same as its own init...
    """
    activities_html = _activities_panel_html(request, trip, profile)
    viewer_membership = None if trip.creator_id == profile.id else TripMembership.objects.for_trip_and_profile(trip, profile).first()
    header_html = render_to_string(
        request=request,
        template_name="dashboard/partials/trips/trip_header_partial.html",
        context={
            "trip": trip,
            "profile": profile,
            "viewer_is_organizer": _is_organizer(profile, trip),
            "viewer_membership": viewer_membership,
            "viewer_has_joined": trip.creator_id == profile.id or (viewer_membership is not None and viewer_membership.status == TripMembership.STATUS_JOINED),
        },
    )
    response = HttpResponse(activities_html + f'<div id="trip-header" hx-swap-oob="true">{header_html}</div>' + _trip_hero_oob(request, trip))
    response["HX-Trigger"] = "activityChanged"
    return response


class TripOverviewView(LoginRequiredMixin, View):
    """Trips section landing page: stats, a small calendar, and recent trips.

    GET /trips/  → overview page
    """

    #: Max trips shown in each of the overview's "recently updated"/"recently viewed" lists.
    RECENT_TRIPS_LIMIT = 5

    def get(self, request):
        from urbanlens.dashboard.models.calendar_sync.model import GoogleCalendarAccount
        from urbanlens.dashboard.services.social.connections import get_connections
        from urbanlens.dashboard.services.trips.trip_calendar import trip_month

        profile, _ = Profile.objects.get_or_create(user=request.user)
        recently_updated_trips = list(Trip.objects.recently_updated(profile, limit=self.RECENT_TRIPS_LIMIT))
        recently_viewed_trips = list(Trip.objects.recently_viewed(profile, limit=self.RECENT_TRIPS_LIMIT))
        # Matches TripListView/CalendarImportView - every list of other members' trips must mask identities the
        # viewer isn't allowed to see. Only the two rendered lists need it.
        _apply_trip_list_identity_masking(profile, recently_updated_trips)
        _apply_trip_list_identity_masking(profile, recently_viewed_trips)
        return render(
            request,
            "dashboard/pages/trips/overview.html",
            {
                "profile": profile,
                "page_name": "trips",
                "stats": Trip.objects.filter(profiles=profile).timeline_counts(),
                "trip_month": trip_month(profile, timezone.now().date()),
                "recently_updated_trips": recently_updated_trips,
                "recently_viewed_trips": recently_viewed_trips,
                "calendar_account": GoogleCalendarAccount.objects.get_for_profile(profile),
                "friends": get_connections(profile),
            },
        )


class TripListView(LoginRequiredMixin, View):
    """Trips list page and trip creation.

    GET  /trips/list/   → list page
    POST /trips/create/ → create a new trip, return updated list partial
    """

    def get(self, request):
        from urbanlens.dashboard.models.calendar_sync.model import GoogleCalendarAccount
        from urbanlens.dashboard.services.social.connections import get_connections

        profile, _ = Profile.objects.get_or_create(user=request.user)
        context = trip_list_context(request, profile)
        if request.headers.get("HX-Request") == "true":
            return render(request, "dashboard/partials/trips/trip_list_partial.html", context)
        return render(
            request,
            "dashboard/pages/trips/index.html",
            {
                **context,
                "page_name": "trips",
                "friends": get_connections(profile),
                "calendar_account": GoogleCalendarAccount.objects.get_for_profile(profile),
            },
        )


class TripCalendarView(LoginRequiredMixin, View):
    """Trips calendar page: a month view of the viewer's trips.

    GET /trips/calendar/?month=YYYY-MM  → calendar page
    """

    def get(self, request):
        from urbanlens.dashboard.services.trips.trip_calendar import parse_month, trip_month

        profile, _ = Profile.objects.get_or_create(user=request.user)
        month = parse_month(request.GET.get("month"), timezone.now().date())
        return render(
            request,
            "dashboard/pages/trips/calendar.html",
            {
                "profile": profile,
                "page_name": "trips",
                "trip_month": trip_month(profile, month),
            },
        )


class TripCalendarMonthView(LoginRequiredMixin, View):
    """One month of the trips calendar, for the prev/next controls.

    GET /trips/calendar/month/?month=YYYY-MM  → the calendar partial
    """

    def get(self, request):
        from urbanlens.dashboard.services.trips.trip_calendar import parse_month, trip_month

        profile, _ = Profile.objects.get_or_create(user=request.user)
        month = parse_month(request.GET.get("month"), timezone.now().date())
        return render(request, "dashboard/partials/trips/_mini_calendar.html", {"trip_month": trip_month(profile, month)})


class TripPickerView(LoginRequiredMixin, View):
    """The viewer's trips matching a name fragment, as picker rows.

    GET /trips/picker/?q=<fragment>  → at most ``LIMIT`` ``<li>`` rows, most recently updated first
    """

    LIMIT = 20

    def get(self, request):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        query = (request.GET.get("q") or "").strip()
        trips = list(Trip.objects.search_for_member(profile, query, limit=self.LIMIT + 1))
        return render(
            request,
            "dashboard/partials/trips/_trip_picker_rows.html",
            {"trips": trips[: self.LIMIT], "truncated": len(trips) > self.LIMIT, "query": query},
        )


class TripCreateView(LoginRequiredMixin, View):
    """Create a new trip.

    POST /trips/create/  → re-renders the trip list partial
    """

    def post(self, request):
        from django.urls import reverse

        profile, _ = Profile.objects.get_or_create(user=request.user)

        try:
            body = json.loads(request.body) if request.body else {}
            invite_ids = body.get("invite_profile_ids") or []
        except (json.JSONDecodeError, ValueError):
            body = request.POST.dict()
            invite_ids = request.POST.getlist("invite_profile_ids")

        source = body.get("source") or "list"
        invite_emails = parse_address_list(body.get("invite_emails"))
        malformed = next((address for address in invite_emails if not is_valid_address(address)), None)
        if malformed is not None:
            return HttpResponse(f'"{escape(malformed)}" isn\'t a valid email address.', status=400)

        try:
            trip, _created = create_trip(
                profile,
                name=body.get("name"),
                description=body.get("description"),
                start_date=body.get("start_date"),
                end_date=body.get("end_date"),
                invite_profile_ids=invite_ids,
            )
        except TripError as exc:
            return _trip_error_response(exc)

        for address in invite_emails:
            try:
                invite_to_trip_by_email(trip, profile, address, invitation_url_builder=request.build_absolute_uri)
            except TripError as exc:
                messages.warning(request, f"Not every invitation was sent: {exc.message}")
                break

        if source == "overview":
            response = HttpResponse("", status=200)
            response["HX-Redirect"] = reverse("trips.detail", kwargs={"trip_slug": trip.slug})
            return response

        return render(request, "dashboard/partials/trips/trip_list_partial.html", trip_list_context(request, profile))


class TripDetailView(LoginRequiredMixin, View):
    """Trip detail page.

    GET /trips/<slug>/
    """

    def get(self, request, trip_slug):
        from urbanlens.dashboard.controllers.calendar_sync import calendar_context

        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = trip_or_not_found(request, trip_slug, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result
        viewer_membership = None if trip.creator_id == profile.id else TripMembership.objects.for_trip_and_profile(trip, profile).first()
        TripMembership.objects.for_trip_and_profile(trip, profile).update(last_viewed_at=timezone.now())
        return render(
            request,
            "dashboard/pages/trips/detail.html",
            {
                "trip": trip,
                "profile": profile,
                "page_name": "trip-detail",
                "viewer_is_organizer": _is_organizer(profile, trip),
                "viewer_membership": viewer_membership,
                "viewer_has_joined": _viewer_has_joined(profile, trip),
                **calendar_context(profile, trip),
                **profile.get_map_center_template_context(),
                "default_map_view": profile.default_map_view,
                "map_dark_mode": profile.map_dark_mode,
                "show_map_footer": True,
            },
        )


class TripEditView(LoginRequiredMixin, View):
    """Edit trip metadata.

    POST /trips/<slug>/edit/  → returns updated trip header partial
    """

    def post(self, request, trip_slug):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = trip_or_not_found(request, trip_slug, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result

        try:
            body = json.loads(request.body) if request.body else {}
        except (json.JSONDecodeError, ValueError):
            body = request.POST.dict()

        try:
            # Presence-keyed: only submitted fields are touched. A blank name is
            # ignored while a blank description clears - see update_trip.
            trip = update_trip(trip, profile, changes={key: body[key] for key in ("name", "description", "start_date", "end_date") if key in body})
        except TripError as exc:
            return _trip_error_response(exc)

        from urbanlens.dashboard.controllers.calendar_sync import calendar_context

        header_html = render_to_string(
            request=request,
            template_name="dashboard/partials/trips/trip_header_partial.html",
            context={
                "trip": trip,
                "profile": profile,
                "viewer_is_organizer": _is_organizer(profile, trip),
                "viewer_membership": None,
                "viewer_has_joined": True,
                **calendar_context(profile, trip),
            },
        )
        return HttpResponse(header_html + _trip_hero_oob(request, trip))


class TripDeleteView(LoginRequiredMixin, View):
    """Delete a trip (creator only).

    DELETE /trips/<slug>/delete/
    """

    def delete(self, request, trip_slug):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = trip_or_not_found(request, trip_slug, profile)
        if isinstance(result, HttpResponse):
            return result
        try:
            delete_trip(result, profile)
        except TripError as exc:
            return _trip_error_response(exc)
        response = HttpResponse("", status=200)
        response["HX-Trigger"] = json.dumps({"showToast": {"level": "success", "message": TRIP_DELETED_MESSAGE}})
        return response


class TripActivitiesView(LoginRequiredMixin, View):
    """Activities panel for a trip.

    GET  /trips/<slug>/activities/  → render panel
    POST /trips/<slug>/activities/  → add activity, re-render panel
    """

    def get(self, request, trip_slug):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = trip_or_not_found(request, trip_slug, profile)
        if isinstance(result, HttpResponse):
            return result
        return _render_activities_panel(request, result, profile)

    def post(self, request, trip_slug):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = trip_or_not_found(request, trip_slug, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result

        try:
            body = json.loads(request.body) if request.body else {}
        except (json.JSONDecodeError, ValueError):
            body = request.POST.dict()

        try:
            create_activity(
                trip,
                profile,
                title=body.get("title"),
                notes=body.get("notes"),
                scheduled_at=_parse_scheduled_at(body.get("scheduled_date"), body.get("scheduled_time")),
                scheduled_end=_parse_scheduled_at(body.get("scheduled_end_date"), body.get("scheduled_end_time")),
                place=body,
                child_trip_uuid=body.get("child_trip_uuid"),
                status=body.get("status"),
                location_hidden=body.get("location_hidden"),
            )
        except TripError as exc:
            return _trip_error_response(exc)

        return _render_activities_panel(request, trip, profile)


class TripAiSuggestionsView(LoginRequiredMixin, View):
    """AI-generated trip suggestions: pins worth adding, and a possible reorder.

    GET /trips/<slug>/ai-suggestions/ -> render panel (cached)
    POST /trips/<slug>/ai-suggestions/ -> force a fresh generation (cooldown-limited)

    Read-only: this view never creates or changes anything by itself.
    Adding a suggested pin re-uses the normal add-activity endpoint (the suggestion already carries the
    requester's own pin slug); applying a suggested order is a separate, explicit action (see
    TripApplySuggestedOrderView).
    """

    def get(self, request, trip_slug):
        return self._respond(request, trip_slug, force_refresh=False)

    def post(self, request, trip_slug):
        return self._respond(request, trip_slug, force_refresh=True)

    def _respond(self, request, trip_slug, *, force_refresh: bool):
        from urbanlens.dashboard.services.trips.trip_ai_suggestions import get_trip_suggestions

        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = trip_or_not_found(request, trip_slug, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result

        if not _viewer_has_joined(profile, trip):
            return HttpResponse("Join this trip to see suggestions.", status=403)

        suggestions = get_trip_suggestions(trip, profile, force_refresh=force_refresh)
        return render(
            request,
            "dashboard/partials/trips/_trip_ai_suggestions_panel.html",
            {"trip": trip, "profile": profile, "suggestions": suggestions},
        )


class TripApplySuggestedOrderView(LoginRequiredMixin, View):
    """Apply an AI-suggested activity order.

    POST /trips/<slug>/activities/apply-order/

    Body: {"order": [activity_id, ...]} Only ever accepts an exact permutation of the trip's own current
    non-completed activities - never partial, never containing another trip's ids, so a stale or
    tampered order can't silently drop or hijack activities.
    """

    def post(self, request, trip_slug):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = trip_or_not_found(request, trip_slug, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result

        try:
            body = json.loads(request.body) if request.body else {}
        except (json.JSONDecodeError, ValueError):
            body = request.POST.dict()

        from urbanlens.dashboard.models.site_settings import SiteSettings
        from urbanlens.dashboard.services.core.reorder_limits import UNLIMITED_TRIP_FALLBACK, reorder_id_ceiling

        submitted = body.get("order") or []
        # The trip's own activity limit, for the same reason the list reorder
        # uses its pin limit - and counted before the values are converted.
        ceiling = reorder_id_ceiling(SiteSettings.get_current().max_trip_activities, UNLIMITED_TRIP_FALLBACK)
        if len(submitted) > ceiling:
            return HttpResponse(f"Reorder at most {ceiling} activities at a time.", status=400)

        try:
            order = [int(value) for value in submitted]
        except (TypeError, ValueError):
            return HttpResponse("Invalid order.", status=400)

        try:
            reorder_activities(trip, profile, order)
        except TripError as exc:
            return _trip_error_response(exc)

        return _render_activities_panel(request, trip, profile)


class TripActivityEditView(LoginRequiredMixin, View):
    """Edit a trip activity.

    POST /trips/<slug>/activities/<int:activity_id>/edit/  → re-render panel
    """

    def post(self, request, trip_slug, activity_id):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = trip_or_not_found(request, trip_slug, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result

        try:
            body = json.loads(request.body) if request.body else {}
        except (json.JSONDecodeError, ValueError):
            body = request.POST.dict()

        # The edit form always submits every field, so the presence-keyed service call reproduces this
        # endpoint's full-replace semantics - except child_trip_uuid, which the form only sends when it applies.
        changes: dict[str, Any] = {
            "title": body.get("title"),
            "notes": body.get("notes"),
            "scheduled_at": _parse_scheduled_at(body.get("scheduled_date"), body.get("scheduled_time")),
            "scheduled_end": _parse_scheduled_at(body.get("scheduled_end_date"), body.get("scheduled_end_time")),
            "place": body,
            "status": body.get("status"),
            "location_hidden": body.get("location_hidden"),
        }
        if "child_trip_uuid" in body:
            changes["child_trip_uuid"] = body.get("child_trip_uuid")

        try:
            update_activity(trip, profile, activity_id, changes=changes)
        except TripError as exc:
            return _trip_error_response(exc)

        return _render_activities_panel(request, trip, profile)


class TripActivityDeleteView(LoginRequiredMixin, View):
    """Delete a single activity and re-render the activities panel.

    DELETE /trips/<slug>/activities/<int:activity_id>/delete/
    """

    def delete(self, request, trip_slug, activity_id):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = trip_or_not_found(request, trip_slug, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result

        try:
            delete_activity(trip, profile, activity_id)
        except TripError as exc:
            return _trip_error_response(exc)
        return _render_activities_panel(request, trip, profile)


class TripActivityCompleteView(LoginRequiredMixin, View):
    """Mark an activity as completed, snapping its date to today if it was in the future.

    POST /trips/<slug>/activities/<int:activity_id>/complete/
    """

    def post(self, request, trip_slug, activity_id):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = trip_or_not_found(request, trip_slug, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result

        completed_date_str = request.POST.get("completed_date", "")
        try:
            completed_date = datetime.date.fromisoformat(completed_date_str) if completed_date_str else None
        except ValueError:
            completed_date = None

        try:
            complete_activity(trip, profile, activity_id, completed_date=completed_date)
        except TripError as exc:
            return _trip_error_response(exc)

        return _render_activities_panel(request, trip, profile)


class TripActivityVoteView(LoginRequiredMixin, View):
    """Cast, update, or clear a member's vote on a proposed activity.

    POST /trips/<slug>/activities/<int:activity_id>/vote/
    Form body: vote=up | vote=down | vote= (empty to clear)
    """

    def post(self, request, trip_slug, activity_id):
        """Handle a vote submission and re-render the activities panel.

        Args:
            request: The HTTP request.
            trip_slug: The trip URL slug.
            activity_id: The activity ID.

        Returns:
            Re-rendered activities panel or an error response.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = trip_or_not_found(request, trip_slug, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result

        try:
            set_activity_vote(trip, profile, activity_id, vote=request.POST.get("vote", "").strip() or None)
        except TripError as exc:
            return _trip_error_response(exc)

        return _render_activities_panel(request, trip, profile)


#: Top-level comments per page in the trip comments panel, matching the
#: pin/wiki panel's own page size.
_TRIP_COMMENTS_PAGE_SIZE = 8


def _render_trip_comments(request: HttpRequest, trip: Trip, profile: Profile) -> HttpResponse:
    """Re-render the comments panel from the shared visible-comment tree.

    Args:
        request: The incoming request.
        trip: The trip whose comments are being rendered.
        profile: The viewing profile.

    Returns:
        The rendered comments panel.
    """
    from django.urls import reverse

    from urbanlens.dashboard.services.core.pagination import get_page
    from urbanlens.dashboard.services.trips.trip_comments import visible_comment_count, visible_comment_queryset

    # Default to the last page: comments run oldest to newest, so the most
    # recent activity is what a viewer should see without paging back.
    page_obj = get_page(request, visible_comment_queryset(trip, profile), _TRIP_COMMENTS_PAGE_SIZE, default_last=True)
    rendered: list[TripCommentData] = build_comment_tree(trip, profile, comments=page_obj.object_list)
    return render(
        request,
        "dashboard/partials/trips/trip_comments_panel.html",
        {
            "trip": trip,
            "rendered_comments": rendered,
            "page_obj": page_obj,
            # The panel partial is also rendered inside the full trip page, so
            # the pagination bar cannot fall back to request.path - a click
            # there would swap a whole document into the panel.
            "comments_url": reverse("trips.comments", args=[trip.slug]),
            # The whole visible thread, not the page - a badge taken from the
            # page would shrink the moment paging started.
            "comment_count": visible_comment_count(trip, profile),
            "profile": profile,
            "allowed_emojis": ALLOWED_COMMENT_EMOJIS,
            "viewer_has_joined": _viewer_has_joined(profile, trip),
        },
    )


class TripCommentsView(LoginRequiredMixin, View):
    """Comments panel for a trip.

    GET  /trips/<slug>/comments/  → render panel
    POST /trips/<slug>/comments/  → add comment, re-render panel
    """

    def get(self, request, trip_slug):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = trip_or_not_found(request, trip_slug, profile)
        if isinstance(result, HttpResponse):
            return result
        return _render_trip_comments(request, result, profile)

    def post(self, request, trip_slug):
        from urbanlens.dashboard.controllers.comments import _parse_map_data

        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = trip_or_not_found(request, trip_slug, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result

        try:
            add_comment(
                trip,
                profile,
                text=request.POST.get("text", ""),
                parent_id=request.POST.get("parent_id"),
                image=request.FILES.get("image"),
                existing_image_id=request.POST.get("existing_image_id", "").strip(),
                map_data=_parse_map_data(request),
            )
        except TripError as exc:
            return _trip_error_response(exc)

        return _render_trip_comments(request, trip, profile)


class TripCommentDeleteView(LoginRequiredMixin, View):
    """Delete a comment (author or trip creator only).

    DELETE /trips/<slug>/comments/<int:comment_id>/delete/
    """

    def delete(self, request, trip_slug, comment_id):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = trip_or_not_found(request, trip_slug, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result
        try:
            delete_comment(trip, profile, get_comment(trip, comment_id))
        except TripError as exc:
            return _trip_error_response(exc)
        return _render_trip_comments(request, trip, profile)


class TripMembersView(LoginRequiredMixin, View):
    """Members panel for a trip.

    GET  /trips/<slug>/members/  → render panel
    POST /trips/<slug>/members/  → add a member by username or invite an email address, re-render panel
    """

    def get(self, request, trip_slug):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = trip_or_not_found(request, trip_slug, profile)
        if isinstance(result, HttpResponse):
            return result
        return _render_members_panel(request, result, profile)

    def post(self, request, trip_slug):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = trip_or_not_found(request, trip_slug, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result

        try:
            body = json.loads(request.body) if request.body else {}
        except (json.JSONDecodeError, ValueError):
            body = request.POST.dict()

        username = (body.get("username") or "").strip()
        email = (body.get("email") or "").strip() or (username if "@" in username else "")
        try:
            if email:
                invite_to_trip_by_email(trip, profile, email, invitation_url_builder=request.build_absolute_uri)
            else:
                add_member_by_username(trip, profile, username)
        except TripError as exc:
            return _trip_error_response(exc)

        response = _render_members_panel(request, trip, profile)
        if email:
            response["HX-Trigger"] = json.dumps({"showToast": {"level": "success", "message": "Invitation sent."}})
        return response


class TripMemberRemoveView(LoginRequiredMixin, View):
    """Remove a member from a trip.

    DELETE /trips/<slug>/members/<int:profile_id>/remove/
    Only the trip creator may remove members (members can remove themselves).
    """

    def delete(self, request, trip_slug, profile_id):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = trip_or_not_found(request, trip_slug, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result
        try:
            # Scoped to this trip's roster: the old global Profile lookup let a
            # member probe arbitrary profile ids for existence.
            remove_member(trip, profile, resolve_trip_member(trip, profile_id=profile_id))
        except TripError as exc:
            return _trip_error_response(exc)

        return _render_members_panel(request, trip, profile)


class TripMemberOrganizerView(LoginRequiredMixin, View):
    """Toggle organizer status for a trip member (creator only).

    POST /trips/<slug>/members/<int:profile_id>/organizer/
    """

    def post(self, request, trip_slug, profile_id):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = trip_or_not_found(request, trip_slug, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result

        try:
            # Scoped to this trip's roster - see TripMemberRemoveView.
            require_trip_creator(trip, profile)
            target = resolve_trip_member(trip, profile_id=profile_id)
            membership = TripMembership.objects.for_trip_and_profile(trip, target).first()
            set_member_organizer(trip, profile, target, is_organizer=not (membership is not None and membership.is_organizer))
        except TripError as exc:
            return _trip_error_response(exc)

        # Organizer status feeds directly into each activity's can_manage flag (see _activities_panel_html) -
        # without this, the acting creator (and the newly (de)promoted organizer, on their own screen) wouldn't
        # see activity permissions update until reloading.
        members_response = _render_members_panel(request, trip, profile)
        activities_html = _activities_panel_html(request, trip, profile, oob=True)
        members_response.content += activities_html.encode()
        return members_response


class TripMapDataView(LoginRequiredMixin, View):
    """Return GeoJSON-style activity data for the trip map.

    GET /trips/<slug>/map-data/
    """

    def get(self, request, trip_slug):
        """Return activity locations with coordinates as JSON.

        Args:
            request: The HTTP request.
            trip_slug: The trip URL slug.

        Returns:
            JsonResponse with a list of activity points.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = trip_or_not_found(request, trip_slug, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result
        include_past = request.GET.get("include_past", "0") not in {"", "0", "false"}
        # The external API's map endpoint returns exactly this, unmodified -
        # see services.trips.trip_map for why the two must not diverge.
        return JsonResponse({"points": build_trip_map_points(trip, profile, include_past=include_past)})


class TripActivityStatusView(LoginRequiredMixin, View):
    """Toggle or set activity status (proposed/confirmed).

    POST /trips/<slug>/activities/<int:activity_id>/status/
    Body: {status: "proposed"|"confirmed"}
    """

    def post(self, request, trip_slug, activity_id):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = trip_or_not_found(request, trip_slug, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result

        try:
            body = json.loads(request.body) if request.body else {}
        except (json.JSONDecodeError, ValueError):
            body = request.POST.dict()

        try:
            # An absent/unrecognized status toggles, preserving this endpoint's
            # "click to flip" behavior.
            set_activity_status(trip, profile, activity_id, status=body.get("status"))
        except TripError as exc:
            return _trip_error_response(exc)

        return _render_activities_panel(request, trip, profile)


class TripActivityMoveView(LoginRequiredMixin, View):
    """Update the date of an activity (calendar drag-and-drop).

    POST /trips/<slug>/activities/<int:activity_id>/move/
    Body: {date: "YYYY-MM-DD"}
    """

    def post(self, request, trip_slug, activity_id):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = trip_or_not_found(request, trip_slug, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result

        try:
            body = json.loads(request.body) if request.body else {}
        except (json.JSONDecodeError, ValueError):
            body = request.POST.dict()

        date_str = (body.get("date") or "").strip()
        if not date_str:
            return HttpResponse("date is required.", status=400)

        try:
            new_date = datetime.date.fromisoformat(date_str)
        except ValueError:
            return HttpResponse("Invalid date format.", status=400)

        # See services.trips.trip_activities.set_activity_position's docstring for the same class of fix made
        # there earlier.
        try:
            move_activity(trip, profile, activity_id, date=new_date)
        except TripError as exc:
            return _trip_error_response(exc)

        return _render_activities_panel(request, trip, profile)


class TripMembershipJoinView(LoginRequiredMixin, View):
    """Accept a trip invitation.

    POST /trips/<slug>/join/

    Unlocks contribution rights (add/edit activities, comment, vote, add members) for an invited member

    - separate from RSVP, which only says whether they expect to actually show up. Declining an
      invitation reuses `TripLeaveView` instead, since a not-yet-joined m...
    """

    def post(self, request, trip_slug):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = trip_or_not_found(request, trip_slug, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result

        join_trip(trip, profile)

        # Joining unlocks contribution across the whole page (activities, comments, members) - simplest to
        # reload rather than stitch together OOB swaps for every affected panel for a rare, one-off action.
        response = HttpResponse("", status=200)
        response["HX-Refresh"] = "true"
        return response


class TripMemberRSVPView(LoginRequiredMixin, View):
    """Set RSVP status for the current user on a trip.

    POST /trips/<slug>/rsvp/
    Body: {rsvp: "yes"|"no"|"maybe"|""}
    """

    def post(self, request, trip_slug):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = trip_or_not_found(request, trip_slug, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result

        try:
            body = json.loads(request.body) if request.body else {}
        except (json.JSONDecodeError, ValueError):
            body = request.POST.dict()

        try:
            set_trip_rsvp(trip, profile, body.get("rsvp"))
        except TripError as exc:
            return _trip_error_response(exc)

        members_response = _render_members_panel(request, trip, profile)
        return HttpResponse(members_response.content + _activities_panel_html(request, trip, profile, oob=True).encode())


class TripActivityRSVPView(LoginRequiredMixin, View):
    """Set or clear the current user's RSVP override for one activity.

    POST /trips/<slug>/activities/<id>/rsvp/

    Body: {rsvp: "yes"|"no"|"maybe"|""} An empty value deletes the override so the activity immediately
    inherits the current trip RSVP again.
    """

    def post(self, request, trip_slug, activity_id):
        """Persist the activity RSVP override.

        Args:
            request: The incoming HTTP request.
            trip_slug: Slug of the containing trip.
            activity_id: Primary key of the activity being answered.

        Returns:
            Refreshed activities-panel HTML, or an error response.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = trip_or_not_found(request, trip_slug, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result
        try:
            body = json.loads(request.body) if request.body else {}
        except (json.JSONDecodeError, ValueError):
            body = request.POST.dict()

        try:
            set_activity_rsvp(trip, profile, activity_id, rsvp=body.get("rsvp"))
        except TripError as exc:
            return _trip_error_response(exc)

        return HttpResponse(_activities_panel_html(request, trip, profile))


class TripLeaveView(LoginRequiredMixin, View):
    """Leave a trip (non-creator members only).

    DELETE /trips/<slug>/leave/

    Also doubles as "decline invitation" for a member who was invited but never joined (see
    `TripMembershipJoinView`) - either way the membership row is simply removed.
    """

    def delete(self, request, trip_slug):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = trip_or_not_found(request, trip_slug, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result

        try:
            leave_trip(trip, profile)
        except TripError as exc:
            return _trip_error_response(exc)

        from django.urls import reverse as _reverse

        response = HttpResponse("", status=200)
        response["HX-Redirect"] = _reverse("trips.list")
        return response


class TripSettingsView(LoginRequiredMixin, View):
    """Save trip settings (creator only).

    POST /trips/<slug>/settings/
    """

    def post(self, request, trip_slug):
        """Handle POST to update trip permission settings.

        Args:
            request: The HTTP request.
            trip_slug: The trip URL slug.

        Returns:
            Rendered settings partial on success, or an error HttpResponse.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = trip_or_not_found(request, trip_slug, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result

        try:
            trip = set_trip_permissions(trip, profile, changes=request.POST.dict())
        except TripError as exc:
            return _trip_error_response(exc)

        return render(
            request,
            "dashboard/partials/trips/trip_settings_partial.html",
            {
                "trip": trip,
                "profile": profile,
                "saved": True,
            },
        )


class TripActivityPositionView(LoginRequiredMixin, View):
    """Save a map-drag position override for a trip activity.

    POST /trips/<slug>/activities/<int:activity_id>/position/

    Body: {lat: float, lng: float} This updates lat_override/lng_override on the TripActivity only - the
    underlying Pin and Location coordinates are never modified.
    """

    def post(self, request, trip_slug, activity_id):
        """Handle POST to update map position override.

        Args:
            request: The HTTP request.
            trip_slug: The trip URL slug.
            activity_id: The TripActivity primary key.

        Returns:
            JsonResponse confirming saved coordinates, or an error HttpResponse.
            Note: See ``services.trips.trip_activities.set_activity_position``.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = trip_or_not_found(request, trip_slug, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result

        try:
            body = json.loads(request.body) if request.body else {}
        except (json.JSONDecodeError, ValueError):
            body = request.POST.dict()

        if "lat" not in body or "lng" not in body:
            return HttpResponse("lat and lng are required.", status=400)

        try:
            lat, lng = set_activity_position(trip, profile, activity_id, lat=body["lat"], lng=body["lng"])
        except TripError as exc:
            return _trip_error_response(exc)

        return JsonResponse({"lat": lat, "lng": lng})


class TripChildTripSearchView(LoginRequiredMixin, View):
    """Search for trips the current user can add as a child activity.

    GET /trips/<slug>/child-trip-search/?q=<query>

    Only trips the user is a member of (excluding the current trip) are returned.
    """

    def get(self, request, trip_slug):
        """Return JSON list of matching trips.

        Args:
            request: The HTTP request.
            trip_slug: The parent trip's URL slug (to exclude it from results).

        Returns:
            JsonResponse with a list of matching trip objects.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        q = (request.GET.get("q") or "").strip()
        if len(q) < 2:
            return JsonResponse({"results": []})

        trips = Trip.objects.filter(profiles=profile, name__icontains=q).exclude(slug=trip_slug).with_effective_dates().order_by("name")[:8]
        results = [
            {
                "uuid": str(t.uuid),
                "name": t.name,
                "start_date": t.effective_start_date.isoformat() if t.effective_start_date else None,
                "end_date": t.effective_end_date.isoformat() if t.effective_end_date else None,
            }
            for t in trips
        ]
        return JsonResponse({"results": results})


def _forecast_gap_seconds(slot: ForecastSlot, target: datetime.datetime) -> float:
    """Absolute gap in seconds between a forecast slot and a scheduled time.

    Slots without one fall back to comparing wall clocks, forced naive on both sides so an
    offset-carrying ``date`` can't raise "can't subtract offset-naive and offset-aware datetimes" and
    500 the trip page.

    Args:
        slot: The forecast slot to measure.
        target: The activity's scheduled time (aware UTC from the ORM, but a naive value is tolerated
        and falls back to the wall-clock path).

    Returns:
        The absolute difference in seconds.
    """
    date_utc = slot.get("date_utc")
    if date_utc is not None and target.tzinfo is not None:
        return abs((date_utc - target).total_seconds())
    slot_date = slot["date"]
    if slot_date.tzinfo is not None:
        slot_date = slot_date.replace(tzinfo=None)
    target_naive = target.replace(tzinfo=None) if target.tzinfo is not None else target
    return abs((slot_date - target_naive).total_seconds())


#: How far ahead an activity can be and still be matched to a forecast: the longest forecast any provider here
#: publishes (Open-Meteo's 16 days) plus the gap a slot may sit from its activity. Nothing later is fetched.
FORECAST_HORIZON = datetime.timedelta(days=16, hours=36)

#: How long one place's forecast is reused.
FORECAST_CACHE_TTL = 3600

#: The widest gap between an activity and the slot describing it.
_MAX_SLOT_GAP_HOURS = 36


def _forecast_cell(coords: tuple[float, float]) -> tuple[float, float]:
    return round(coords[0], 2), round(coords[1], 2)


def _fetch_forecasts(cells: list[tuple[float, float]], *, caller: str | None) -> tuple[dict[tuple[float, float], list[ForecastSlot]], bool]:
    """Forecast slots for each place, from the cache or one fetch under the forecast upstream's policy.

    The missing places are fetched one after another in a single background call holding one slot, each
    cached as it arrives, so a fetch that outlives the request still fills the cache for the next one.

    Args:
        cells: Rounded coordinates to forecast.
        caller: Who to charge against the per-account rate, or None.

    Returns:
        The slots per place that could be had now, and whether asking again shortly may find more.
    """
    from urbanlens.dashboard.services.apis.locations.weather_resolution import get_raw_forecast_slots
    from urbanlens.dashboard.services.apis.request_upstreams import WeatherForecastUpstream

    found: dict[tuple[float, float], list[ForecastSlot]] = {}
    missing: list[tuple[float, float]] = []
    for cell in cells:
        hit = WeatherForecastUpstream.cached(f"{cell[0]:.2f}:{cell[1]:.2f}")
        if hit is not None:
            found[cell] = hit
        else:
            missing.append(cell)
    if not missing:
        return found, False

    budget = WeatherForecastUpstream.deadline * 3

    def fetch() -> dict[tuple[float, float], list[ForecastSlot]]:
        started = time.monotonic()
        fetched: dict[tuple[float, float], list[ForecastSlot]] = {}
        for cell in missing:
            if time.monotonic() - started > budget:
                break
            try:
                slots = get_raw_forecast_slots(*cell)
            except (requests.RequestException, GatewayRequestError) as exc:
                logger.warning("Weather fetch failed for a trip activity's location: %s", type(exc).__name__)
                continue
            if slots:
                WeatherForecastUpstream.store(f"{cell[0]:.2f}:{cell[1]:.2f}", slots, FORECAST_CACHE_TTL)
                fetched[cell] = slots
        return fetched

    result = WeatherForecastUpstream.call(fetch, caller=caller)
    found.update(result.value_or({}))
    return found, not result.ok


def _build_activity_forecasts(activities: list[TripActivity], *, caller: str | None = None) -> list[dict]:
    """For each activity, find the closest forecast slot at its location and time.

    Tries REData first, then the direct OpenWeatherMap/Open-Meteo chain - see
    ``services.apis.locations.weather_resolution.get_raw_forecast_slots``.

    Args:
        activities: The activities to forecast; the caller leaves out any past :data:`FORECAST_HORIZON`.
        caller: Who to charge against the forecast upstream's per-account rate, or None.

    Returns:
        One dict per activity with ``activity``, ``location_name``, ``scheduled_at``, ``slot``, ``no_coords``,
        ``out_of_range`` and ``pending`` (its place's forecast may arrive if asked again shortly).
    """
    coords_by_activity = {id(act): activity_coords(act) for act in activities}
    cells = sorted({_forecast_cell(coords) for act in activities if (coords := coords_by_activity[id(act)]) is not None and act.scheduled_at is not None})
    slots_by_cell, pending = _fetch_forecasts(cells, caller=caller) if cells else ({}, False)

    results = []
    for act in activities:
        coords = coords_by_activity[id(act)]
        location_name = act.effective_title if act.effective_title != "Unnamed activity" else ""
        entry: dict = {
            "activity": act,
            "location_name": location_name,
            "scheduled_at": act.scheduled_at,
            "slot": None,
            "no_coords": coords is None,
            "out_of_range": False,
            "pending": False,
        }
        if coords is None or act.scheduled_at is None:
            results.append(entry)
            continue

        slots = slots_by_cell.get(_forecast_cell(coords)) or []
        if not slots:
            entry["pending"] = pending
            results.append(entry)
            continue

        closest, gap_seconds = min(((slot, _forecast_gap_seconds(slot, act.scheduled_at)) for slot in slots), key=lambda pair: pair[1])
        if gap_seconds / 3600 > _MAX_SLOT_GAP_HOURS:
            entry["out_of_range"] = True
        else:
            entry["slot"] = closest
        results.append(entry)

    return results


def _group_by_day(rows: list[dict]) -> list[tuple]:
    """Bucket weather rows by their activity's calendar day, earliest first.

    Args:
        rows: Dicts carrying a ``scheduled_at``.

    Returns:
        ``[(day, rows)]`` with undated rows, if any, last.
    """
    from collections import defaultdict

    day_map: dict = defaultdict(list)
    for row in rows:
        day_map[row["scheduled_at"].date() if row["scheduled_at"] else None].append(row)
    dated = sorted(day for day in day_map if day is not None)
    keys = dated + ([None] if None in day_map else [])
    return [(day, day_map[day]) for day in keys]


def _build_activity_history(activities: list[TripActivity]) -> tuple[list[dict], bool]:
    """For each past activity, what the weather actually was on its day, from stored rows.

    Missing days are queued for a background fetch rather than fetched here, the way the visit-history panel
    does it; a record of a past day never changes, so once stored it needs no freshness handling.

    Args:
        activities: Past trip activities, in any order.

    Returns:
        Dicts with ``activity``, ``location_name``, ``scheduled_at`` and ``recorded`` (a ``RecordedDay``),
        earliest first, and whether some wanted days are not stored yet.
    """
    from urbanlens.dashboard.services.apis.locations.redata_context_gateway import redata_configured
    from urbanlens.dashboard.services.locations.visit_weather import cached_records, convert_records, missing_days, queue_missing_days, weather_cell

    by_cell: dict[tuple[int, int], list[TripActivity]] = {}
    for act in activities:
        coords = activity_coords(act)
        if coords is None or act.scheduled_at is None:
            continue
        by_cell.setdefault(weather_cell(*coords), []).append(act)

    wanted = {cell: [act.scheduled_at.date() for act in group if act.scheduled_at is not None] for cell, group in by_cell.items()}
    stored = cached_records(wanted)
    queued = False
    for cell, days in wanted.items():
        if missing := missing_days(days, stored[cell]):
            queued = queue_missing_days(cell, missing) or queued
    if queued:
        # Where Celery runs eagerly (tests, a single-process dev stack) the days are already stored.
        stored = cached_records(wanted)

    results: list[dict] = []
    fetchable = redata_configured()
    pending = False
    for cell, group in by_cell.items():
        days = wanted[cell]
        pending = pending or (fetchable and bool(missing_days(days, stored[cell])))
        recorded = convert_records(stored[cell], days)
        for act in group:
            if act.scheduled_at is None:
                continue
            entry = recorded.get(act.scheduled_at.date().isoformat())
            if entry is None or not entry.has_readings:
                continue
            location_name = act.effective_title if act.effective_title != "Unnamed activity" else ""
            results.append({"activity": act, "location_name": location_name, "scheduled_at": act.scheduled_at, "recorded": entry})

    results.sort(key=lambda row: row["scheduled_at"])
    return results, pending


#: Per account. Each render can fetch a forecast per activity day.
TRIP_WEATHER_RATE = Rate(limit=30, window_seconds=60, on_outage=Outage.REFUSE)
TRIP_WEATHER_METHODS = frozenset({"GET"})


class TripWeatherView(LoginRequiredMixin, View):
    """Render the weather forecast panel for a trip.

    GET /trips/<slug>/weather/
    """

    def get(self, request, trip_slug):
        """Return weather HTML partial for the trip.

        Args:
            request: The HTTP request.
            trip_slug: The trip URL slug.

        Returns:
            Rendered weather partial or an error response.
        """
        from collections import defaultdict

        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = trip_or_not_found(request, trip_slug, profile)
        if isinstance(result, HttpResponse):
            return result
        trip = result

        error: str = ""
        grouped: list[tuple] = []
        recorded_days: list[tuple] = []
        pending = False

        if not profile.external_apis_enabled:
            error = "External weather lookups are turned off in your settings."
        else:
            from urbanlens.dashboard.services.security.throttle import account_or_address

            today = timezone.localdate()
            horizon = timezone.now() + FORECAST_HORIZON
            all_activities = list(_activity_qs(trip))
            # A past activity is one the forecast can no longer speak to.
            past_activities = [act for act in all_activities if act.scheduled_at is not None and act.scheduled_at.date() < today]
            try:
                recorded, pending = _build_activity_history(past_activities)
            except (KeyError, TypeError, ValueError):
                # Stored rows are read and missing days queued, so no outage reaches here - only a malformed
                # activity (an unparseable coordinate, say).
                logger.warning("Historical weather lookup failed for trip %s", trip_slug, exc_info=True)
                recorded = []
            recorded_days = _group_by_day(recorded)

            activities = [act for act in all_activities if act.status != TripActivity.STATUS_COMPLETED and act.scheduled_at is not None and today <= act.scheduled_at.date() and act.scheduled_at <= horizon]
            if not activities:
                pass  # no upcoming activities - leave error/grouped empty to hide the section
            else:
                try:
                    activity_forecasts = _build_activity_forecasts(activities, caller=account_or_address(request))
                    pending = pending or any(af["pending"] for af in activity_forecasts)
                    # Drop activities with nothing useful to show (no location data, or too far outside the
                    # 5-day forecast window) instead of rendering an empty "No location data"/"Outside 5-day
                    # forecast" row for them - a day (or the whole panel) with nothing left after this simply
                    # doesn't appear, rather than showing only empty placeholders.
                    activity_forecasts = [af for af in activity_forecasts if af["slot"] is not None]

                    day_map: dict = defaultdict(list)
                    for af in activity_forecasts:
                        day = af["scheduled_at"].date() if af["scheduled_at"] else None
                        day_map[day].append(af)

                    dated = sorted(d for d in day_map if d is not None)
                    keys = dated + ([None] if None in day_map else [])
                    grouped = [(d, day_map[d]) for d in keys]
                except (requests.RequestException, KeyError, TypeError):
                    logger.warning("Weather fetch failed for trip %s", trip_slug)
                    error = "Weather data could not be loaded."

        return render(
            request,
            "dashboard/pages/trips/trip_weather.html",
            {
                "trip": trip,
                "grouped": grouped,
                "recorded_days": recorded_days,
                "error": error,
                # Asked once more after a pause while something is still arriving; the retry never asks again, so a
                # day REData cannot answer does not poll forever.
                "refresh_url": f"{request.path}?retry=1" if pending and not request.GET.get("retry") else "",
            },
        )
