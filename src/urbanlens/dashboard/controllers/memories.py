"""Memories page controllers - map/timeline aggregation of routes, trips, visits, photos."""

from __future__ import annotations

import datetime
import json
import logging
from typing import TYPE_CHECKING, Any, TypedDict

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import DateField, Min
from django.db.models.functions import Cast, Coalesce
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views import View

from urbanlens.dashboard.controllers.visits import (
    _parse_visited_at,
    _resolve_participants,
    _sync_visit_photos,
    _visit_dialog_context,
)
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.markup.model import MarkupMap
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.routes.model import Route
from urbanlens.dashboard.models.trips.model import Trip, TripMembership
from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource
from urbanlens.dashboard.services.map_snapshot import materialize_markup_map, parse_map_data
from urbanlens.dashboard.services.memories.aggregator import BBox, get_memory_events
from urbanlens.dashboard.services.memories.distance import total_travel_distance_km
from urbanlens.dashboard.services.memories.journal import get_journal_entries
from urbanlens.dashboard.services.memories.unlogged import unlogged_visited_pins
from urbanlens.dashboard.services.units import km_to_display, unit_label
from urbanlens.dashboard.services.visit_invites import resolve_suggest_participant_ids, sync_external_participants
from urbanlens.dashboard.services.visits import add_visited_status, create_visit_suggestion, remove_visited_status, sync_last_visited, visit_logging_allowed

if TYPE_CHECKING:
    from django.http import HttpRequest

    from urbanlens.dashboard.models.markup.share import MarkupMapShare
    from urbanlens.dashboard.models.pin_share.model import PinShare

logger = logging.getLogger(__name__)

_DEFAULT_WINDOW_DAYS = 90
_ON_THIS_DAY_LIMIT = 10


class _ShareGroup(TypedDict):
    """One pin's entry in the Sharing page's ``share_groups`` list."""

    pin: Pin
    shares: list[PinShare]
    chain_total: int
    reshare_count: int


class _MapShareGroup(TypedDict):
    """One map's entry in the Sharing page's ``map_share_groups`` list."""

    map: MarkupMap
    shares: list[MarkupMapShare]
    attachment_label: str | None
    attachment_url: str | None


class _IncomingShareGroup(TypedDict):
    """One pin's entry in the Sharing page's ``incoming_share_groups`` list.

    Unlike :class:`_ShareGroup`, there's no chain/reshare count here - the
    chain machinery is rooted at the *sender's* side, and the recipient only
    ever sees their own inbound shares of a given pin.
    """

    pin: Pin
    shares: list[PinShare]


class _IncomingMapShareGroup(TypedDict):
    """One map's entry in the Sharing page's ``incoming_map_share_groups`` list."""

    map: MarkupMap
    shares: list[MarkupMapShare]


def _attachment_label_url(kind: str, host: Any, *, markup_map: MarkupMap) -> tuple[str | None, str | None]:
    """Resolve a human label and link for one (kind, host) attachment entry.

    Args:
        kind: One of ``safety_checkin`` / ``comment`` / ``trip_comment`` /
            ``visit`` / ``direct_message``.
        host: The attached instance matching *kind*.
        markup_map: The map being described - only needed to tell which side
            of a ``direct_message`` is "the other person" (this map's owner
            sent it, so the label always names the recipient).

    Returns:
        Tuple of (label, url), or (None, None) when the host can't be
        resolved to a link (e.g. a comment on a wiki with no location).
    """
    if kind == "safety_checkin":
        return f"Safety check-in: {host.title}", reverse("safety.checkin.detail", args=[host.slug or host.uuid])
    if kind == "comment" and host.pin_id:
        return f"Comment on {host.pin.effective_name}", reverse("pin.details", args=[host.pin.slug])
    if kind == "comment":
        return f"Comment on {host.wiki.name}", reverse("location.wiki", args=[host.wiki.location.slug])
    if kind == "trip_comment":
        return f"Comment on trip: {host.trip.name}", reverse("trips.detail", args=[host.trip.slug])
    if kind == "visit":
        label = f"Visit to {host.pin.effective_name} on {host.visited_at:%b} {host.visited_at.day}, {host.visited_at.year}"
        return label, reverse("pin.details", args=[host.pin.slug])
    if kind == "direct_message":
        recipient = host.recipient if host.sender_id == markup_map.profile_id else host.sender
        return f"Direct message to {recipient.username}", reverse("messages.conversation", args=[recipient.slug])
    return None, None


def _map_attachment_info(markup_map: MarkupMap) -> tuple[str | None, str | None]:
    """Resolve a human label and link for the map's single "primary" attachment.

    Args:
        markup_map: The map to inspect.

    Returns:
        Tuple of (label, url), or (None, None) when the map is an unattached
        draft.
    """
    attachment = markup_map.attachment
    if attachment is None:
        return None, None
    kind, host = attachment
    return _attachment_label_url(kind, host, markup_map=markup_map)


def _map_attachment_entries(markup_map: MarkupMap) -> list[dict[str, str]]:
    """Resolve a label + link for every place a map is currently attached to.

    Unlike :func:`_map_attachment_info` (the single "primary" link shown
    under a map's title), this lists every comment, trip comment, safety
    check-in, visit, and direct message referencing the map - so the owner
    can see (and jump to) each one before deleting it.

    Args:
        markup_map: The map to inspect.

    Returns:
        List of ``{"label": ..., "url": ...}`` dicts, one per attachment.
    """
    entries: list[dict[str, str]] = []
    for kind, host in markup_map.attachments:
        label, url = _attachment_label_url(kind, host, markup_map=markup_map)
        if label:
            entries.append({"label": label, "url": url or ""})
    return entries


def _unlogged_band_context(profile: Profile) -> dict[str, object]:
    """Build the context the unlogged-visits band needs, shared by every view that renders it.

    Args:
        profile: The viewing profile whose visited-but-unlogged pins to surface.

    Returns:
        Context dict with ``unlogged_visits`` (the pins) and ``today`` (an ISO
        date string used to bound and prefill the quick-log date inputs).
    """
    return {
        "unlogged_visits": unlogged_visited_pins(profile),
        "today": timezone.now().date().isoformat(),
    }


def _toast(message: str, level: str = "success", *, unlogged_count: int | None = None) -> HttpResponse:
    """Return an empty HTMX response that removes the swapped card and fires a toast.

    Args:
        message: Text to display in the toast.
        level: toastr level (``success``/``info``/``warning``/``error``).
        unlogged_count: When given, also tells the shared _photos_tabs.html nav
            (outside this card's own swap target) to update or remove its
            "Visits" tab label instead of leaving it stale.

    Returns:
        An empty-body response carrying an ``HX-Trigger`` header; swapping it with
        ``outerHTML`` removes the card while the toast fires.
    """
    triggers: dict[str, object] = {"showToast": {"message": message, "level": level}}
    if unlogged_count is not None:
        triggers["unloggedVisitsCountChanged"] = {"count": unlogged_count}
    response = HttpResponse("")
    response["HX-Trigger"] = json.dumps(triggers)
    return response


def _parse_date(value: str | None, default: datetime.date) -> datetime.date:
    """Parse an ISO date query param, falling back to *default* if missing/invalid."""
    if not value:
        return default
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        return default


def _parse_bbox(request: HttpRequest) -> BBox | None:
    """Parse a ``minLat,minLng,maxLat,maxLng`` bbox query param, or None if absent/invalid."""
    raw = request.GET.get("bbox")
    if not raw:
        return None
    try:
        min_lat, min_lng, max_lat, max_lng = (float(part) for part in raw.split(","))
    except ValueError:
        return None
    return BBox(min_lat, min_lng, max_lat, max_lng)


def _active_span(earliest: datetime.date | None, today: datetime.date) -> tuple[int, str]:
    """Describe how long a profile has been active in the largest sensible time unit.

    Picks the coarsest unit (years, months, weeks, days) that still yields a
    count of at least one, so a brand-new account whose only memory is a few
    months old reads "3 months active" instead of a misleading "1 years active".

    Args:
        earliest: The earliest date across the profile's memories, or None if it has none.
        today: The current date to measure against.

    Returns:
        A ``(count, unit)`` tuple, e.g. ``(3, "Months")``. The unit is singular
        when ``count`` is 1 (e.g. ``(1, "Day")``).
    """
    if earliest is None:
        return (0, "Days")
    days = max((today - earliest).days, 0)
    if days >= 365:
        count, unit = days // 365, "Year"
    elif days >= 60:
        count, unit = days // 30, "Month"
    elif days >= 14:
        count, unit = days // 7, "Week"
    else:
        count, unit = max(days, 1), "Day"
    return (count, unit if count == 1 else f"{unit}s")


def _earliest_memory_date(profile: Profile) -> datetime.date | None:
    """Return the earliest date across all of a profile's memory sources, for scrubber bounds."""
    candidates: list[datetime.date] = []

    route_started = Route.objects.for_profile(profile).order_by("started_at").values_list("started_at", flat=True).first()
    if route_started:
        candidates.append(route_started.date())

    visit_at = PinVisit.objects.filter(pin__profile=profile).order_by("visited_at").values_list("visited_at", flat=True).first()
    if visit_at:
        candidates.append(visit_at.date())

    photo_at = Image.objects.filter(profile=profile).order_by("created").values_list("created", flat=True).first()
    if photo_at:
        candidates.append(photo_at.date())

    # Mirrors Trip.effective_start_date: explicit start_date wins, else the
    # earliest scheduled activity's date (see services.memories.aggregator._trips_for_range).
    trip_start = (
        Trip.objects.filter(profiles=profile)
        .annotate(_first_activity_date=Cast(Min("activities__scheduled_at"), output_field=DateField()))
        .annotate(_eff_start=Coalesce("start_date", "_first_activity_date"))
        .filter(_eff_start__isnull=False)
        .order_by("_eff_start")
        .values_list("_eff_start", flat=True)
        .first()
    )
    if trip_start:
        candidates.append(trip_start)

    return min(candidates) if candidates else None


def _compute_hero_stats(profile: Profile) -> tuple[dict[str, object], bool]:
    """Build the Memories page's hero-stat tiles (distance, places, photos, trips, active span).

    Shared by the initial page render and ``MemoriesHeroStatsView`` (the latter
    lets the tiles refresh in place after an in-page action like logging a
    visit or adding photos, instead of only updating on the next full reload).

    Args:
        profile: The profile whose memories to summarize.

    Returns:
        (hero_stats context dict for _hero_stats.html, has_memory_data flag).
    """
    route_count = Route.objects.for_profile(profile).count()
    total_distance_km = total_travel_distance_km(profile)
    units = profile.effective_distance_units
    places_visited = PinVisit.objects.filter(pin__profile=profile).values("pin_id").distinct().count()
    photo_count = Image.objects.filter(profile=profile).count()
    trip_count = TripMembership.objects.filter(profile=profile).values("trip_id").distinct().count()
    has_memory_data = any((route_count, places_visited, photo_count, trip_count))

    today = timezone.now().date()
    earliest = _earliest_memory_date(profile)
    active_count, active_unit = _active_span(earliest, today)

    hero_stats = {
        "total_distance": round(km_to_display(total_distance_km, units), 1),
        "distance_unit": unit_label(units),
        "places_visited": places_visited,
        "photo_count": photo_count,
        "trip_count": trip_count,
        "active_count": active_count,
        "active_unit": active_unit,
    }
    return hero_stats, has_memory_data


class MemoriesView(LoginRequiredMixin, View):
    """Memories page - map + timeline of everywhere a profile has been.

    GET /memories/
    """

    def get(self, request: HttpRequest):
        """Render the Memories page with hero stats and scrubber bounds.

        Args:
            request: The HTTP request.

        Returns:
            Rendered Memories page.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)

        hero_stats, has_memory_data = _compute_hero_stats(profile)

        today = timezone.now().date()
        earliest = _earliest_memory_date(profile)

        return render(
            request,
            "dashboard/pages/memories/index.html",
            {
                "profile": profile,
                "page_name": "memories",
                "has_memory_data": has_memory_data,
                "hero_stats": hero_stats,
                "earliest_date": earliest.isoformat() if earliest else today.isoformat(),
                **_unlogged_band_context(profile),
                **profile.get_map_center_template_context(),
                "show_map_footer": True,
            },
        )


class MemoriesHeroStatsView(LoginRequiredMixin, View):
    """HTMX partial - the hero-stat tiles at the top of the Memories page.

    GET /memories/hero-stats/

    Re-fetched via ``memoriesFeedRefresh`` (the same trigger already fired after
    logging a visit or uploading photos) so distance/places/photos/trips/active-span
    stay current after an in-page action, not just on the next full reload.
    """

    def get(self, request: HttpRequest):
        """Render the hero-stats partial.

        Args:
            request: The HTTP request.

        Returns:
            Rendered ``_hero_stats.html`` partial.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        hero_stats, _has_memory_data = _compute_hero_stats(profile)
        return render(request, "dashboard/partials/memories/_hero_stats.html", {"hero_stats": hero_stats})


class MemoriesFeedDataView(LoginRequiredMixin, View):
    """Aggregated map/timeline data for the Memories page.

    GET /memories/data/?start=YYYY-MM-DD&end=YYYY-MM-DD&bbox=minLat,minLng,maxLat,maxLng

    A date range is always applied (defaulting to the trailing 90 days) so a
    profile's full history is never loaded in a single request.
    """

    def get(self, request: HttpRequest):
        """Return MemoryEvents for the requested date range/viewport as JSON.

        Args:
            request: The HTTP request.

        Returns:
            JsonResponse with ``start``, ``end``, and ``events``.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)

        today = timezone.now().date()
        default_start = today - datetime.timedelta(days=_DEFAULT_WINDOW_DAYS)
        start = _parse_date(request.GET.get("start"), default_start)
        end = _parse_date(request.GET.get("end"), today)
        bbox = _parse_bbox(request)

        events = get_memory_events(profile, start, end, bbox=bbox)

        return JsonResponse(
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "events": [
                    {
                        "type": event.type,
                        "occurred_at": event.occurred_at.isoformat(),
                        "ended_at": event.ended_at.isoformat() if event.ended_at else None,
                        "title": event.title,
                        "subtitle": event.subtitle,
                        "latitude": event.latitude,
                        "longitude": event.longitude,
                        "url": event.url,
                        "thumbnail_url": event.thumbnail_url,
                        "icon": event.icon,
                        "color": event.color,
                        "extra": event.extra,
                    }
                    for event in events
                ],
            },
        )


class MemoriesOnThisDayView(LoginRequiredMixin, View):
    """HTMX partial - memories from this month/day in past years.

    GET /memories/on-this-day/
    """

    def get(self, request: HttpRequest):
        """Render the "on this day" callout partial.

        Args:
            request: The HTTP request.

        Returns:
            Rendered partial listing past-year visits/routes/photos on today's month/day.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        today = timezone.now().date()

        visits = list(
            PinVisit.objects.filter(pin__profile=profile, visited_at__month=today.month, visited_at__day=today.day).exclude(visited_at__year=today.year).select_related("pin").order_by("-visited_at")[:_ON_THIS_DAY_LIMIT],
        )
        routes = list(
            Route.objects.for_profile(profile).filter(started_at__month=today.month, started_at__day=today.day).exclude(started_at__year=today.year).order_by("-started_at")[:_ON_THIS_DAY_LIMIT],
        )
        photos = list(
            Image.objects.filter(profile=profile, taken_at__month=today.month, taken_at__day=today.day).exclude(taken_at__year=today.year).select_related("pin", "wiki", "wiki__location").order_by("-taken_at")[:_ON_THIS_DAY_LIMIT],
        )

        return render(
            request,
            "dashboard/partials/memories/_on_this_day.html",
            {"visits": visits, "routes": routes, "photos": photos, "today": today},
        )


class MemoriesVisitView(LoginRequiredMixin, View):
    """Log a dated visit for a marked-but-unlogged pin, or add details to an existing one.

    GET  /memories/visit/<pin_slug>/                 → render the add-visit form (dialog body)
    GET  /memories/visit/<pin_slug>/<visit_id>/      → render the edit-visit form for an existing visit
    POST /memories/visit/<pin_slug>/                 → create a new dated PinVisit
    POST /memories/visit/<pin_slug>/<visit_id>/      → update an existing PinVisit

    Both POST variants reuse the pin-detail visit form's field handling (date,
    notes, photos, map snapshot, participants) and return the refreshed
    unlogged-visits band plus an ``HX-Trigger`` that toasts and reloads the
    timeline feed.
    """

    def _get_pin(self, request: HttpRequest, pin_slug: str) -> tuple[Pin, Profile]:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        pin = get_object_or_404(Pin, slug=pin_slug, profile=profile)
        return pin, profile

    def get(self, request: HttpRequest, pin_slug: str, visit_id: int | None = None) -> HttpResponse:
        """Render the visit form for the shared Memories dialog.

        Args:
            request: The HTTP request.
            pin_slug: Slug of the pin the visit belongs to.
            visit_id: PK of an existing visit to edit, or None to add a new one.

        Returns:
            The rendered ``_visit_form.html`` partial, wired to post back to this
            view and swap the unlogged-visits band.
        """
        pin, _ = self._get_pin(request, pin_slug)
        visit = get_object_or_404(PinVisit.objects.prefetch_related("participants", "images"), id=visit_id, pin=pin) if visit_id else None

        context = _visit_dialog_context(pin)
        context.update(
            {
                "visit": visit,
                "dialog_id": "memories-visit-dialog",
                "form_action": reverse("memories.visit.edit", args=[pin.slug, visit.id]) if visit else reverse("memories.visit", args=[pin.slug]),
                "form_target": "#memories-unlogged-band",
                "form_swap": "outerHTML",
                # New visits default their date to when the pin was marked visited,
                # falling back to today, so the common case is a single click.
                "default_date": "" if visit else (pin.last_visited or timezone.now()).date().isoformat(),
            },
        )
        return render(request, "dashboard/partials/pins/_visit_form.html", context)

    def post(self, request: HttpRequest, pin_slug: str, visit_id: int | None = None) -> HttpResponse:
        """Create or update a dated PinVisit and return the refreshed band.

        Args:
            request: The HTTP request. The body carries ``visited_date`` (required)
                and optional ``visited_time``, ``notes``, ``map_data``, ``photos``,
                ``existing_photo_ids``, and ``participant_ids``.
            pin_slug: Slug of the pin the visit belongs to.
            visit_id: PK of the visit to update, or None to create a new one.

        Returns:
            The re-rendered unlogged-visits band, or a 400 on a missing/invalid date.
        """
        pin, profile = self._get_pin(request, pin_slug)

        visited_at = _parse_visited_at(request)
        if visited_at is None:
            return HttpResponse("A valid date is required.", status=400)

        notes = request.POST.get("notes", "").strip() or None
        map_data = parse_map_data(request)

        if visit_id:
            visit = get_object_or_404(PinVisit, id=visit_id, pin=pin)
            visit.visited_at = visited_at
            visit.notes = notes
            visit.markup_map = materialize_markup_map(profile, map_data, existing_map=visit.markup_map)
            visit.save()
            created = False
        else:
            if not visit_logging_allowed(profile):
                return HttpResponse("Visit logging is turned off - enable it in Settings to log a visit.", status=403)
            visit = PinVisit.objects.create(
                pin=pin,
                visited_at=visited_at,
                notes=notes,
                source=VisitSource.MANUAL,
                markup_map=materialize_markup_map(profile, map_data),
            )
            add_visited_status(pin)
            created = True

        sync_last_visited(pin)
        _sync_visit_photos(request, pin, visit)

        participants = _resolve_participants(request, pin)
        visit.participants.set(participants)

        # On a brand-new visit, offer the tagged connections their own suggestion,
        # mirroring the pin-detail "log a visit" flow. Each participant has an
        # individual "send them a suggestion" toggle in the form.
        suggest_ids = resolve_suggest_participant_ids(request)
        lat, lng = pin.effective_latitude, pin.effective_longitude
        if created and participants and lat is not None and lng is not None:
            for participant in participants:
                if participant.pk not in suggest_ids:
                    continue
                others = [p for p in participants if p.pk != participant.pk]
                create_visit_suggestion(
                    suggested_to=participant,
                    suggested_by=profile,
                    visited_at=visited_at,
                    location=pin.location,
                    latitude=lat,
                    longitude=lng,
                    candidate_profiles=others,
                    origin_visit=visit,
                    origin_pin=pin,
                )

        sync_external_participants(request, visit)

        band_context = _unlogged_band_context(profile)
        response = render(
            request,
            "dashboard/partials/memories/_unlogged_visits.html",
            band_context,
        )
        response["HX-Trigger"] = json.dumps(
            {
                "showToast": {"message": "Visit logged." if created else "Details saved.", "level": "success"},
                "memoriesFeedRefresh": True,
                # The "Visits" tab label lives outside #memories-unlogged-band, in
                # the shared _photos_tabs.html nav - tell it to catch up.
                "unloggedVisitsCountChanged": {"count": len(unlogged_visited_pins(profile))},
            },
        )
        return response


class MemoriesVisitsView(LoginRequiredMixin, View):
    """The "log your visits" subpage of Memories - pins marked visited with no dated record.

    GET /memories/visits/
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        """Render the unlogged-visits page.

        Args:
            request: The HTTP request.

        Returns:
            Rendered Visits page listing every visited-but-unlogged pin.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        return render(
            request,
            "dashboard/pages/memories/visits.html",
            {
                "profile": profile,
                "page_name": "memories",
                **_unlogged_band_context(profile),
            },
        )


class MemoriesSharingView(LoginRequiredMixin, View):
    """The "Sharing" subpage of Memories - every pin and map shared to/from the user.

    Groups the profile's sent :class:`PinShare` rows by pin, listing who each
    pin was shared with, and how far the share travelled: the chain count
    follows reshares transitively (A→B, B→C and B→D, D→E and D→F counts 5
    shares for A's pin). Sent :class:`MarkupMapShare` rows are grouped by map
    the same way, minus the reshare-chain machinery PinShare has.

    Also lists the mirror image - pins and maps *received* from other
    profiles - grouped the same way but without any chain/reshare counts
    (those are rooted at the sender's side) and linking through the
    recipient-scoped share-detail routes rather than the sender's own
    pin/map pages, which the recipient has no access to.

    GET /memories/sharing/
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        """Render the sharing history page.

        Args:
            request: The HTTP request.

        Returns:
            Rendered Sharing page listing every shared pin and map with
            their recipients (and, for pins, chain-wide reshare counts).
        """
        from urbanlens.dashboard.models.markup.share import MarkupMapShare
        from urbanlens.dashboard.models.pin_share.model import PinShare

        profile, _ = Profile.objects.get_or_create(user=request.user)
        shares = PinShare.objects.filter(from_profile=profile).select_related("pin__location__wiki", "to_profile__user").order_by("-created")

        shares_by_pin: dict[int, list[PinShare]] = {}
        for share in shares:
            shares_by_pin.setdefault(share.pin_id, []).append(share)

        share_groups: list[_ShareGroup] = []
        for pin_shares in shares_by_pin.values():
            own_ids = [share.pk for share in pin_shares]
            chain_total = PinShare.chain_share_count(own_ids)
            share_groups.append(
                {
                    "pin": pin_shares[0].pin,
                    "shares": pin_shares,
                    "chain_total": chain_total,
                    # Shares made further down the chain by other users.
                    "reshare_count": chain_total - len(own_ids),
                },
            )

        map_shares = MarkupMapShare.objects.filter(from_profile=profile).select_related("markup_map", "to_profile__user").order_by("-created")

        map_shares_by_map: dict[int, list[MarkupMapShare]] = {}
        for map_share in map_shares:
            map_shares_by_map.setdefault(map_share.markup_map_id, []).append(map_share)

        map_share_groups: list[_MapShareGroup] = []
        for map_shares_for_map in map_shares_by_map.values():
            markup_map = map_shares_for_map[0].markup_map
            label, url = _map_attachment_info(markup_map)
            map_share_groups.append(
                {
                    "map": markup_map,
                    "shares": map_shares_for_map,
                    "attachment_label": label,
                    "attachment_url": url,
                },
            )

        incoming_shares = PinShare.objects.filter(to_profile=profile).select_related("pin__location__wiki", "from_profile__user").order_by("-created")

        incoming_shares_by_pin: dict[int, list[PinShare]] = {}
        for share in incoming_shares:
            incoming_shares_by_pin.setdefault(share.pin_id, []).append(share)

        incoming_share_groups: list[_IncomingShareGroup] = [{"pin": pin_shares[0].pin, "shares": pin_shares} for pin_shares in incoming_shares_by_pin.values()]

        incoming_map_shares = MarkupMapShare.objects.filter(to_profile=profile).select_related("markup_map", "from_profile__user").order_by("-created")

        incoming_map_shares_by_map: dict[int, list[MarkupMapShare]] = {}
        for map_share in incoming_map_shares:
            incoming_map_shares_by_map.setdefault(map_share.markup_map_id, []).append(map_share)

        incoming_map_share_groups: list[_IncomingMapShareGroup] = [{"map": map_shares_for_map[0].markup_map, "shares": map_shares_for_map} for map_shares_for_map in incoming_map_shares_by_map.values()]

        return render(
            request,
            "dashboard/pages/memories/sharing.html",
            {
                "profile": profile,
                "page_name": "memories",
                "share_groups": share_groups,
                "map_share_groups": map_share_groups,
                "sent_count": len(shares) + len(map_shares),
                "incoming_share_groups": incoming_share_groups,
                "incoming_map_share_groups": incoming_map_share_groups,
                "received_count": len(incoming_shares) + len(incoming_map_shares),
                **_unlogged_band_context(profile),
            },
        )


class MemoriesMapsView(LoginRequiredMixin, View):
    """The "Maps" subpage of Memories - every markup map the user has drawn.

    Lists the profile's :class:`MarkupMap` rows (check-in routes, comment and
    visit maps, plus unattached drafts) with thumbnails, a link to whatever
    each map is attached to, and a delete action.

    GET /memories/maps/
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        """Render the markup maps page.

        Args:
            request: The HTTP request.

        Returns:
            Rendered Maps page listing every markup map the user created.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        maps = MarkupMap.objects.for_profile(profile).select_related("shared_by__user").prefetch_related("items").order_by("-updated")
        return render(
            request,
            "dashboard/pages/memories/maps.html",
            {
                "profile": profile,
                "page_name": "memories",
                "map_cards": [self._card(markup_map) for markup_map in maps],
            },
        )

    def _card(self, markup_map: MarkupMap) -> dict[str, object]:
        """Build the template context for one map card.

        Args:
            markup_map: The map to describe.

        Returns:
            Dict with the map, its snapshot (for the Leaflet thumbnail), item
            count, the primary attachment's label + link, and the full list
            of every place (comments, trip comments, check-ins, visits, DMs)
            the map is currently attached to.
        """
        label, url = _map_attachment_info(markup_map)
        attachments = _map_attachment_entries(markup_map)
        return {
            "map": markup_map,
            "snapshot": markup_map.to_snapshot(),
            "item_count": len(markup_map.items.all()),
            "attachment_label": label,
            "attachment_url": url,
            "attachments": attachments,
        }


class MemoriesJournalView(LoginRequiredMixin, View):
    """The "Journal" subpage of Memories - visit notes, ratings, and comments by date.

    Merges every visit the profile added notes to, every pin they've rated,
    and every comment they've posted (on pins, wikis, or trips) into a single
    feed sorted newest-first, so a profile's own written history reads like a
    diary instead of being scattered across pin/wiki/trip pages.

    GET /memories/journal/
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        """Render the journal page.

        Args:
            request: The HTTP request.

        Returns:
            Rendered Journal page listing the profile's visit notes, ratings,
            and comments, newest first.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        return render(
            request,
            "dashboard/pages/memories/journal.html",
            {
                "profile": profile,
                "page_name": "memories",
                "journal_entries": get_journal_entries(profile),
                **_unlogged_band_context(profile),
            },
        )


class MemoriesUnloggedActionView(LoginRequiredMixin, View):
    """Dismiss or un-mark a card in the "log your visits" queue.

    POST /memories/unlogged/<pin_slug>/<action>/
    where action is "dismiss" (hide the suggestion without changing the pin's
    visited status) or "unmark" (clear the pin's visited status entirely, e.g.
    a stray "Visited" label or import glitch the user never actually visited).
    """

    def _get_pin(self, request: HttpRequest, pin_slug: str) -> Pin:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        return get_object_or_404(Pin, slug=pin_slug, profile=profile)

    def dismiss(self, request: HttpRequest, pin: Pin) -> HttpResponse:
        """Hide the pin from the unlogged-visits queue without touching its visited status."""
        Pin.objects.filter(pk=pin.pk).update(unlogged_visit_dismissed=True)
        return _toast("Removed from your list.", "info", unlogged_count=len(unlogged_visited_pins(pin.profile)))

    def unmark(self, request: HttpRequest, pin: Pin) -> HttpResponse:
        """Clear the pin's "Visited" status/label so it drops out of the queue entirely."""
        remove_visited_status(pin)
        return _toast('"Visited" status removed.', "info", unlogged_count=len(unlogged_visited_pins(pin.profile)))

    _ACTIONS = {"dismiss": dismiss, "unmark": unmark}

    def post(self, request: HttpRequest, pin_slug: str, action: str) -> HttpResponse:
        """Dispatch to the handler named by ``action``.

        Args:
            request: The HTTP request.
            pin_slug: Slug of the pin being acted on.
            action: The queue action to perform.

        Returns:
            An HTMX card-removing response, or 404 for an unknown action.
        """
        handler = self._ACTIONS.get(action)
        if handler is None:
            raise Http404
        pin = self._get_pin(request, pin_slug)
        return handler(self, request, pin)
