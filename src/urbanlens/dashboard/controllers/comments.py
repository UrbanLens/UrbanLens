"""Comment and Reaction controllers for Pin and Location (wiki) pages."""

from __future__ import annotations

import json
import logging
from typing import TypedDict

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import NoReverseMatch, reverse
from django.views import View

from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.notifications.meta import NotificationType
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.reactions.model import Reaction
from urbanlens.dashboard.services.mentions import render_comment_text, viewer_pinned_uuids

logger = logging.getLogger(__name__)

_ALLOWED_EMOJIS = {"👍", "👎", "❤️", "😂", "😮", "😢", "🔥", "🏚️"}


def _profile(request) -> Profile:
    profile, _ = Profile.objects.get_or_create(user=request.user)
    return profile


def _render_comments(request, context: dict) -> HttpResponse:
    return render(request, "dashboard/partials/comment_panel.html", context)


def _build_context(comments_qs, profile: Profile, **extra) -> dict:
    pinned = viewer_pinned_uuids(profile)
    top_level = list(
        comments_qs.filter(parent__isnull=True)
        .select_related("profile__user")
        .prefetch_related(
            "reactions__profile",
            "replies__reactions__profile",
            "replies__profile__user",
        ),
    )
    # Collect all unique commenter profiles so we can check photo visibility once.
    all_commenters: set[Profile] = set()
    for c in top_level:
        all_commenters.add(c.profile)
        all_commenters.update(r.profile for r in c.replies.all())
    # Set of profile IDs whose images should be blurred for this viewer.
    blurred_profiles: set[int] = {p.pk for p in all_commenters if p != profile and not profile.can_view_photos_from(p)}

    rendered = []
    for c in top_level:
        html = render_comment_text(c.text, pinned)
        if html is None:
            continue
        replies_rendered = []
        for r in c.replies.all():
            r_html = render_comment_text(r.text, pinned)
            if r_html is None:
                continue
            replies_rendered.append(
                {
                    "comment": r,
                    "rendered_text": r_html,
                    "reactions": _aggregate_reactions(r.reactions.all()),
                },
            )
        rendered.append(
            {
                "comment": c,
                "rendered_text": html,
                "reactions": _aggregate_reactions(c.reactions.all()),
                "replies": replies_rendered,
            },
        )
    return {
        "rendered_comments": rendered,
        "profile": profile,
        "blurred_profiles": blurred_profiles,
        "allowed_emojis": sorted(_ALLOWED_EMOJIS),
        **extra,
    }


# ── Pin comments ──────────────────────────────────────────────────────────────


class PinCommentsView(LoginRequiredMixin, View):
    """GET/POST comment panel for a Pin."""

    def get(self, request, pin_slug):
        from urbanlens.dashboard.models.pin.model import Pin

        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        profile = _profile(request)
        ctx = _build_context(pin.comments.all(), profile, pin=pin, context_type="pin")
        return _render_comments(request, ctx)

    def post(self, request, pin_slug):
        from urbanlens.dashboard.models.pin.model import Pin

        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        profile = _profile(request)
        text = request.POST.get("text", "").strip()
        image = request.FILES.get("image")
        map_data = _parse_map_data(request)
        if not text and not image and not map_data:
            return HttpResponse("Please add some text, a photo, or a map.", status=400)
        parent_id = request.POST.get("parent_id")
        parent = None
        if parent_id:
            parent = get_object_or_404(Comment, id=parent_id, pin=pin)
        comment = Comment.objects.create(pin=pin, profile=profile, text=text, parent=parent, map_data=map_data)
        if image:
            comment.image = image
            comment.save(update_fields=["image"])
        if parent and parent.profile != profile:
            _notify_reply(profile, parent, reply=comment)
        ctx = _build_context(pin.comments.all(), profile, pin=pin, context_type="pin")
        return _render_comments(request, ctx)


class PinCommentDeleteView(LoginRequiredMixin, View):
    """DELETE /map/pin/<uuid>/comments/<int>/delete/"""

    def delete(self, request, pin_slug, comment_id):
        from urbanlens.dashboard.models.pin.model import Pin

        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        profile = _profile(request)
        comment = get_object_or_404(Comment, id=comment_id, pin=pin)
        if comment.profile != profile:
            return HttpResponse("Forbidden", status=403)
        comment.delete()
        return HttpResponse("", status=200)


# ── Wiki (Location) comments ──────────────────────────────────────────────────


class WikiCommentsView(LoginRequiredMixin, View):
    """GET/POST comment panel for a Location wiki."""

    def _get_location_and_profile(self, request, location_slug):
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.pin.model import Pin

        location = get_object_or_404(Location, slug=location_slug)
        profile = _profile(request)
        # Must have this location pinned to comment on its wiki
        if not Pin.objects.filter(profile=profile, location=location).exists():
            return None, None, location
        return profile, location, location

    def get(self, request, location_slug):
        profile, location, _loc = self._get_location_and_profile(request, location_slug)
        if profile is None:
            return HttpResponse("You must have this location pinned to view wiki comments.", status=403)
        ctx = _build_context(location.comments.all(), profile, location=location, context_type="wiki")
        return _render_comments(request, ctx)

    def post(self, request, location_slug):
        profile, location, _loc = self._get_location_and_profile(request, location_slug)
        if profile is None:
            return HttpResponse("You must have this location pinned to leave a comment.", status=403)
        text = request.POST.get("text", "").strip()
        image = request.FILES.get("image")
        map_data = _parse_map_data(request)
        if not text and not image and not map_data:
            return HttpResponse("Please add some text, a photo, or a map.", status=400)
        parent_id = request.POST.get("parent_id")
        parent = None
        if parent_id:
            parent = get_object_or_404(Comment, id=parent_id, location=location)
        comment = Comment.objects.create(
            location=location, profile=profile, text=text, parent=parent, map_data=map_data,
        )
        if image:
            comment.image = image
            comment.save(update_fields=["image"])
        if parent and parent.profile != profile:
            _notify_reply(profile, parent, reply=comment)
        ctx = _build_context(location.comments.all(), profile, location=location, context_type="wiki")
        return _render_comments(request, ctx)


class WikiCommentDeleteView(LoginRequiredMixin, View):
    """DELETE /location/<uuid>/wiki/comments/<int>/delete/"""

    def delete(self, request, location_slug, comment_id):
        from urbanlens.dashboard.models.location.model import Location

        location = get_object_or_404(Location, slug=location_slug)
        profile = _profile(request)
        comment = get_object_or_404(Comment, id=comment_id, location=location)
        if comment.profile != profile:
            return HttpResponse("Forbidden", status=403)
        comment.delete()
        return HttpResponse("", status=200)


# ── Reactions ──────────────────────────────────────────────────────────────────


class CommentReactionView(LoginRequiredMixin, View):
    """POST /comments/<int>/react/  - toggle an emoji reaction on a Comment."""

    def post(self, request, comment_id):
        profile = _profile(request)
        comment = get_object_or_404(Comment, id=comment_id)
        emoji = request.POST.get("emoji", "")
        if emoji not in _ALLOWED_EMOJIS:
            return HttpResponse("Invalid emoji.", status=400)
        reaction = Reaction.objects.filter(profile=profile, emoji=emoji, comment=comment).first()
        if reaction:
            reaction.delete()
        else:
            Reaction.objects.create(profile=profile, emoji=emoji, comment=comment)
            _notify_reaction(profile, comment)
        return _render_reaction_row(request, comment, profile)


class TripCommentReactionView(LoginRequiredMixin, View):
    """POST /trips/<uuid>/comments/<int>/react/  - toggle reaction on a TripComment."""

    def post(self, request, trip_uuid, comment_id):
        from urbanlens.dashboard.models.trips.model import Trip, TripComment

        profile = _profile(request)
        trip = get_object_or_404(Trip, uuid=trip_uuid)
        comment = get_object_or_404(TripComment, id=comment_id, trip=trip)
        emoji = request.POST.get("emoji", "")
        if emoji not in _ALLOWED_EMOJIS:
            return HttpResponse("Invalid emoji.", status=400)
        reaction = Reaction.objects.filter(profile=profile, emoji=emoji, trip_comment=comment).first()
        if reaction:
            reaction.delete()
        else:
            Reaction.objects.create(profile=profile, emoji=emoji, trip_comment=comment)
            _notify_reaction(profile, comment)
        return _render_trip_reaction_row(request, comment, profile)


def _render_reaction_row(request, comment: Comment, profile: Profile) -> HttpResponse:
    reactions = _aggregate_reactions(comment.reactions.all())
    return render(
        request,
        "dashboard/partials/comment_reactions.html",
        {
            "comment": comment,
            "reactions": reactions,
            "profile": profile,
            "react_url_name": "comment.react",
            "allowed_emojis": _ALLOWED_EMOJIS,
        },
    )


def _render_trip_reaction_row(request, comment, profile: Profile) -> HttpResponse:
    reactions = _aggregate_reactions(comment.reactions.all())
    return render(
        request,
        "dashboard/partials/comment_reactions.html",
        {
            "comment": comment,
            "reactions": reactions,
            "profile": profile,
            "react_url_name": "trips.comment.react",
            "trip_uuid": comment.trip.uuid,
            "allowed_emojis": _ALLOWED_EMOJIS,
        },
    )


class _ReactionData(TypedDict):
    count: int
    reacted_by: list[int]


def _aggregate_reactions(reactions_qs) -> dict[str, _ReactionData]:
    """Group reactions by emoji → {count, reacted_by: list of profile_ids}."""
    result: dict[str, _ReactionData] = {}
    for r in reactions_qs.select_related("profile"):
        if r.emoji not in result:
            result[r.emoji] = {"count": 0, "reacted_by": []}
        result[r.emoji]["count"] += 1
        result[r.emoji]["reacted_by"].append(r.profile_id)
    return result


def _parse_map_data(request) -> dict | None:
    """Extract and validate the map_data JSON blob from a comment POST.

    Args:
        request: The HTTP request.

    Returns:
        Parsed dict if valid map_data was submitted, else None.
    """
    raw = request.POST.get("map_data", "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.warning("Ignoring malformed map_data in comment POST")
        return None
    if not isinstance(data, dict):
        return None
    # Require at least a center coordinate.
    if not (isinstance(data.get("center_lat"), int | float) and isinstance(data.get("center_lng"), int | float)):
        return None
    return data


# ── Comment map pin autocomplete endpoint ────────────────────────────────────


class CommentMapPinsView(LoginRequiredMixin, View):
    """GET /comments/map-pins/?q=… - return user's pins for the comment map center picker."""

    def get(self, request):
        from urbanlens.dashboard.models.pin.model import Pin

        profile = _profile(request)
        q = request.GET.get("q", "").strip().lower()
        qs = Pin.objects.filter(profile=profile).select_related("location")[:200]
        results = []
        for pin in qs:
            lat = pin.effective_latitude
            lng = pin.effective_longitude
            if lat is None or lng is None:
                continue
            name = pin.effective_name or ""
            if q and q not in name.lower():
                continue
            results.append(
                {
                    "uuid": str(pin.uuid),
                    "slug": pin.slug or str(pin.uuid),
                    "name": name,
                    "lat": float(lat),
                    "lng": float(lng),
                    "detail_pins_url": f"/map/pin/{pin.slug or pin.uuid}/detail-pins/json/",
                    "markup_url": f"/map/pin/{pin.slug or pin.uuid}/markup/json/",
                },
            )
        return JsonResponse({"pins": results})


# ── Location autocomplete endpoint ───────────────────────────────────────────


class PinnedLocationsJsonView(LoginRequiredMixin, View):
    """GET /comments/locations/  - return viewer's pinned locations for @mention autocomplete."""

    def get(self, request):
        import json

        from urbanlens.dashboard.models.pin.model import Pin

        profile = _profile(request)
        q = request.GET.get("q", "").strip().lower()
        pins = Pin.objects.filter(profile=profile).exclude(location__isnull=True).select_related("location")[:50]
        results = []
        for pin in pins:
            name = pin.location.name or ""
            if not q or q in name.lower():
                results.append({"uuid": str(pin.location.uuid), "name": name})
        return HttpResponse(json.dumps(results), content_type="application/json")


# ── Notification helpers ──────────────────────────────────────────────────────


def _comment_url(comment) -> str:
    """Return the page URL (with anchor) for a comment or trip comment."""
    anchor = f"#comment-{comment.id}"
    try:
        if hasattr(comment, "trip_id") and comment.trip_id:
            return reverse("trips.detail", kwargs={"trip_uuid": comment.trip.uuid}) + anchor
        if hasattr(comment, "pin_id") and comment.pin_id:
            return reverse("pin.details", kwargs={"pin_slug": comment.pin.slug or str(comment.pin.uuid)}) + anchor
        if hasattr(comment, "location_id") and comment.location_id:
            return reverse("location.wiki", kwargs={"location_slug": comment.location.slug or str(comment.location.uuid)}) + anchor
    except NoReverseMatch:
        logger.warning("Could not build comment URL for comment %s", comment.id)
    return ""


def _notify_reply(actor: Profile, parent_comment, reply=None) -> None:
    recipient = parent_comment.profile if hasattr(parent_comment, "profile") else parent_comment.author
    if recipient is None or recipient == actor:
        return
    url = _comment_url(reply or parent_comment)
    NotificationLog.objects.create(
        profile=recipient,
        notification_type=NotificationType.COMMENT_REPLY,
        title=f"{actor.username} replied to your comment",
        message=f"@{actor.username} replied to your comment.",
        url=url,
    )


def _notify_reaction(actor: Profile, comment) -> None:
    recipient = comment.profile if hasattr(comment, "profile") else comment.author
    if recipient is None or recipient == actor:
        return
    url = _comment_url(comment)
    NotificationLog.objects.create(
        profile=recipient,
        notification_type=NotificationType.COMMENT_LIKED,
        title=f"{actor.username} reacted to your comment",
        message=f"@{actor.username} reacted to your comment.",
        url=url,
    )
