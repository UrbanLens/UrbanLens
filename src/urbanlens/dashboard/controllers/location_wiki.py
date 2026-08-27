"""Wiki controller - community-editable page for a shared place.

Routes are keyed by the Location slug (the stable URL token) but every view
operates on the :class:`~urbanlens.dashboard.models.wiki.model.Wiki` for that
Location. Wikis are user-created (from the pin detail page); these views 404
when the place has no wiki yet.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.gis.geos import GEOSGeometry, MultiPolygon, Polygon
from django.db import transaction
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.views import View

from urbanlens.dashboard.controllers.map_overlays import OVERLAY_UUID_PLACEHOLDER, overlay_payload
from urbanlens.dashboard.controllers.temporal_imagery import TEMPORAL_YEAR_PLACEHOLDER
from urbanlens.dashboard.models.abstract.choices import SecurityLevel
from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryType
from urbanlens.dashboard.models.map_overlay.model import MapImageOverlay
from urbanlens.dashboard.models.markup.model import CustomLayer
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki_edit import WikiEdit
from urbanlens.dashboard.models.wiki_stat_vote import WikiStatField, WikiStatVote
from urbanlens.dashboard.services.core.text_limits import MAX_WIKI_DESCRIPTION_LENGTH, text_length_error
from urbanlens.dashboard.services.geo.boundary_voting import BoundaryVoteError, boundary_vote_context, cast_boundary_vote, has_consensus
from urbanlens.dashboard.services.locations import site_scope
from urbanlens.dashboard.services.locations.temporal_imagery import temporal_slider_years
from urbanlens.dashboard.services.pins.public_pins import PublicVoteError, cast_public_vote, public_vote_context
from urbanlens.dashboard.services.places.ambiguity import competing_wiki_locations
from urbanlens.dashboard.services.places.scope import scope_badge
from urbanlens.dashboard.services.undo.handlers.wiki import MODEL_LABEL as WIKI_MODEL_LABEL, with_wiki_descendants
from urbanlens.dashboard.services.undo.service import stash_for_undo
from urbanlens.dashboard.services.wiki.concealment import visible_rows
from urbanlens.dashboard.services.wiki.wiki_access import resolve_visible_wiki, visible_parent_wiki
from urbanlens.dashboard.services.wiki.wiki_edits import WikiEditValidationError, apply_wiki_edit, revert_edit_fields, revert_wiki_edit, save_edited_fields

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.wiki.model import Wiki

logger = logging.getLogger(__name__)

# Metadata for the four community stat votes (danger / vulnerability / priority /
# rating) shown on the wiki page - the shared-place equivalent of a pin's own
# STAT_FIELD_META (see controllers/pin_edit.py), reworded for a community voice
# since these are a composite of every contributing profile's vote, not one
# person's opinion.
WIKI_STAT_FIELD_META = {
    "danger": {
        "label": "Danger",
        "help": "How hazardous this site feels to explorers - structural risks, environmental hazards, or unsafe conditions (1 = low, 5 = extreme).",
        "modifier": "danger",
        "wide": True,
    },
    "priority": {
        "label": "Priority",
        "help": "How urgently the community feels this place deserves a visit (1 = low, 5 = must visit soon).",
        "modifier": "priority",
        "wide": False,
    },
    "rating": {
        "label": "Rating",
        "help": "The community's overall quality rating for this place.",
        "modifier": "",
        "wide": False,
    },
    "vulnerability": {
        "label": "Vulnerability",
        "help": "How at-risk or fragile this site feels to the community - useful for planning and sharing responsibly.",
        "modifier": "vulnerability",
        "wide": True,
    },
}


def _wiki_stat_context(wiki: Wiki, field: str, profile: Profile | None, *, conceal: bool = False) -> dict:
    """Build the template context for one community stat item.

    Args:
        wiki: The wiki the vote/composite belongs to.
        field: One of :class:`WikiStatField`'s values.
        profile: The viewing profile, used to look up their own vote.
        conceal: Whether this viewer sees the concealed form of the wiki.

    Returns:
        Dict with the composite, the viewer's own vote, and that field's
        label/help/modifier/wide metadata.
    """
    return {
        "field": field,
        "my_vote": WikiStatVote.objects.my_vote(wiki, field, profile),
        "composite": WikiStatVote.objects.composite(wiki, field, viewer_conceals=conceal, viewer=profile),
        **WIKI_STAT_FIELD_META[field],
    }


class LocationWikiView(LoginRequiredMixin, View):
    """Main wiki page for a place.

    GET  /location/<slug>/wiki/  → full wiki page
    """

    def get(self, request, location_slug):
        location, wiki, profile = resolve_visible_wiki(request, location_slug)

        from urbanlens.dashboard.services.wiki.concealment import concealment_active

        conceal = concealment_active(wiki, profile)

        # Only count root pins (not detail pins), and count distinct users.
        # The exact count is never exposed - see services.wiki.community_counts.
        # Shared with the external API's wiki detail payload so the two can't
        # drift on the privacy rules (notably: first_pinned is suppressed
        # entirely while the pin count is too low to display).
        from urbanlens.dashboard.services.wiki.community_counts import wiki_community_summary
        from urbanlens.dashboard.services.wiki.concealment import conceal_rows, conceal_wiki, concealed_community_summary

        # Everything below reads field values through this rather than the live
        # row. It delegates for anything not concealed, so the ordinary path is
        # unchanged and the concealed one cannot forget a field.
        shown = conceal_wiki(wiki, profile)

        community = concealed_community_summary() if conceal else wiki_community_summary(wiki, location)
        pin_count_display = {"is_low": community["pin_count_low"], "value": community["pin_count_approx"]}
        first_pinned = community["first_pinned"]

        # The requesting user's own pin for this location (used for the back-link).
        user_pin = location.pins.filter(profile=profile).first()

        # Places that genuinely compete for the user's pin coordinate - two
        # unrelated parcels whose county geometry overlaps. Not the buildings
        # on this property, which are the same place and answer as one; see
        # services.places.ambiguity for why this is now almost always empty.
        other_locations = [candidate for candidate in competing_wiki_locations(user_pin, profile) if candidate.pk != location.pk]

        from urbanlens.dashboard.models.labels.model import COLOR_CHOICES
        from urbanlens.dashboard.models.pin.model import PinType

        detail_pin_icon_choices = [
            ("place", "Place"),
            ("business", "Building"),
            ("door_front", "Entrance"),
            ("star", "Star"),
            ("warning", "Warning"),
            ("info", "Info"),
            ("camera_alt", "Camera"),
            ("local_parking", "Parking"),
            ("stairs", "Stairs"),
            ("elevator", "Elevator"),
            ("exit_to_app", "Exit"),
            ("lock", "Lock"),
            ("construction", "Construction"),
            ("emergency", "Emergency"),
        ]

        # A cover photo is somebody's photograph, promoted by somebody's choice,
        # and neither the act nor the promotion is recorded anywhere the
        # provenance rules can read. Concealed viewers get the state a wiki has
        # before anyone picks one.
        show_wiki_cover_photo = bool(not conceal and profile.show_wiki_cover_photos and wiki.cover_photo_id)
        wiki_cover_candidates: list[dict] = []
        if show_wiki_cover_photo:
            from urbanlens.dashboard.models.images.model import Image

            wiki_cover_candidates = [{"id": img.pk, "url": img.image.url} for img in Image.objects.filter(wiki=wiki).visible_to(profile).exclude(pk=wiki.cover_photo_id).order_by("-created")[:20] if img.image]

        # Filtered by who created it, not hidden outright - see the matching
        # rule (and reasoning) in controllers.custom_layers._resolve_layer_owner.
        custom_layers = list(visible_rows(CustomLayer.objects.for_wiki(wiki), wiki, profile).order_by("order", "created"))
        visible_layer_ids = {layer.pk for layer in custom_layers}

        return render(
            request,
            "dashboard/pages/location/wiki.html",
            {
                "wiki": shown,
                "custom_layers": custom_layers,
                "custom_layers_json": [layer.to_json() for layer in custom_layers],
                "manage_layers_url": reverse("location.wiki.layers", args=[location.slug]),
                # Only ever the parent this viewer could actually open - see
                # visible_parent_wiki: linking to one they haven't earned would
                # confirm a place exists that they cannot see.
                "parent_wiki": visible_parent_wiki(wiki, profile),
                # Filtered by who created it - own+friends overlays survive, an
                # overlay filed under a now-invisible layer renders unlayered
                # (see overlay_payload's visible_layer_ids).
                "map_overlays_json": overlay_payload(visible_rows(MapImageOverlay.objects.for_wiki(wiki), wiki, profile), visible_layer_ids),
                "manage_overlays_url": reverse("location.wiki.overlays", args=[location.slug]),
                "manage_overlays_historical_url": reverse("location.wiki.overlays.historical", args=[location.slug]),
                "overlay_corners_url_template": reverse("location.wiki.overlays.corners", args=[location.slug, OVERLAY_UUID_PLACEHOLDER]),
                "temporal_slider_years": temporal_slider_years(location, request.user),
                "temporal_imagery_url_template": reverse("location.wiki.temporal_imagery", args=[location.slug, TEMPORAL_YEAR_PLACEHOLDER]),
                "location": location,
                "profile": profile,
                "show_wiki_cover_photo": show_wiki_cover_photo,
                "wiki_cover_candidates": wiki_cover_candidates,
                "is_site_scope": site_scope.is_site_scope(wiki),
                **scope_badge(wiki),
                # Counted over what this viewer can see, not over every row - a
                # badge computed on the raw set announces the comments it is
                # standing in front of.
                "wiki_comment_count": (conceal_rows(wiki.comments.all(), profile) if conceal else wiki.comments.all()).count(),
                "pin_count_display": pin_count_display,
                "first_pinned": first_pinned,
                "wiki_stats": [_wiki_stat_context(wiki, field, profile, conceal=conceal) for field in WikiStatField.values],
                "public_vote": public_vote_context(location, profile, conceal=conceal),
                "boundary_vote": boundary_vote_context(location.place, profile, conceal=conceal),
                "user_pin": user_pin,
                "other_locations": other_locations,
                "page_name": "location-wiki",
                "pin_type_choices": PinType.choices,
                "detail_pin_icon_choices": detail_pin_icon_choices,
                "color_choices": COLOR_CHOICES,
                "markup_fill_color": profile.markup_fill_color,
                "markup_fill_opacity": profile.markup_fill_opacity,
                "markup_border_color": profile.markup_border_color,
                "markup_border_opacity": profile.markup_border_opacity,
                "security_level_choices": SecurityLevel.choices,
                # Read from `shown`: these eight feed both the About-card chips
                # and the always-rendered Suggest-edits prefill, so reading the
                # live row here would put concealed values back on the page
                # through a <select>.
                "location_security_values": [
                    ("fences", "Fences", shown.fences),
                    ("alarms", "Alarms", shown.alarms),
                    ("cameras", "Cameras", shown.cameras),
                    ("security", "Security", shown.security),
                    ("signs", "Signs", shown.signs),
                    ("vps", "VPS", shown.vps),
                    ("plywood", "Plywood", shown.plywood),
                    ("locked", "Locked", shown.locked),
                ],
                "show_map_footer": True,
            },
        )


class WikiBuildingAttributesPanelView(LoginRequiredMixin, View):
    """GET: the wiki's shared Building Attributes card (REData building number/name/year built).

    Location-scoped (``LocationCache``, not a Pin), so this reads whatever
    ``RedataBuildingAttributesEnrichmentSource``'s background enrichment cycle
    (or a visiting pin's own on-demand panel fetch) already cached - the wiki
    page never triggers a live REData fetch itself, mirroring how the
    Ownership card only ever shows already-known ``WikiOwner`` rows.
    """

    def get(self, request: HttpRequest, location_slug: str) -> HttpResponse:
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.plugins.builtin.redata_building_attributes import _render_building_attributes

        location, wiki, _profile = resolve_visible_wiki(request, location_slug)
        # A parcel-scope wiki describes grounds, not a structure - the buildings
        # panel stands in for this card there (see services.locations.site_scope).
        if site_scope.is_site_scope(wiki):
            return HttpResponse(status=204)
        cached = LocationCache.get_fresh(location, "redata_building_attributes")
        if cached is None:
            return HttpResponse(status=204)

        context = _render_building_attributes(cached.data or {})
        if context is None:
            return HttpResponse(status=204)

        return render(
            request,
            "dashboard/partials/pins/_simple_info_panel.html",
            {
                "section_id": "location-building-attributes-panel",
                "icon": "domain",
                "title": "Building Attributes",
                **context,
            },
        )


class WikiParcelBuildingsPanelView(LoginRequiredMixin, View):
    """GET: the wiki's shared list of every building standing on this property.

    The community counterpart to ``PinController.parcel_buildings``, rendering
    the same partial from the same location-scoped ``LocationCache`` row. Like
    the Building Attributes card beside it, this never triggers a live fetch -
    it shows whatever ``ParcelBuildingsEnrichmentSource``'s background cycle
    (or a visiting pin's own panel fetch) already cached.

    Rows never link anywhere: a child wiki is a marker on this page's own map,
    not a page of its own.
    """

    def get(self, request: HttpRequest, location_slug: str) -> HttpResponse:
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.plugins.builtin.parcel_buildings import building_rows

        location, wiki, _profile = resolve_visible_wiki(request, location_slug)
        cached = LocationCache.get_fresh(location, site_scope.PARCEL_BUILDINGS_CACHE_SOURCE)
        if cached is None:
            return HttpResponse(status=204)

        buildings = (cached.data or {}).get("buildings") or []
        if not buildings:
            return HttpResponse(status=204)

        boundary_polygon, boundary_source = Boundary.objects.resolve_for_wiki(wiki, BoundaryType.PROPERTY)
        if boundary_source == "circle":
            boundary_polygon = None

        return render(
            request,
            "dashboard/partials/pins/_parcel_buildings_panel.html",
            {
                "section_id": "location-parcel-buildings-panel",
                "icon": "apartment",
                "title": "Buildings on this Property",
                "rows": building_rows(buildings, list(wiki.child_wikis.select_related("location")), boundary_polygon=boundary_polygon),
            },
        )


class LocationWikiEditView(LoginRequiredMixin, View):
    """Suggest (and immediately apply) a community edit to a Wiki's fields.

    POST /location/<slug>/wiki/edit/
    Body (JSON or form): field=value pairs for any subset of _WIKI_EDITABLE_FIELDS.
    Records a WikiEdit and applies changes to the Wiki.
    """

    def post(self, request, location_slug):
        from urbanlens.dashboard.services.wiki.concealment import conceal_wiki, writable_wiki

        _location, wiki, profile = resolve_visible_wiki(request, location_slug)

        try:
            body = json.loads(request.body) if request.body else {}
        except (json.JSONDecodeError, ValueError):
            body = request.POST.dict()

        # strict=False keeps this view's long-standing skip-invalid-and-continue
        # behavior (see apply_wiki_edit's docstring, and "Messaging / external API
        # (noted 2026-07-26)" in docs/PROBLEMS.md, the strict-vs-lenient item); the
        # external API passes strict=True and gets a hard rejection instead.
        # apply_wiki_edit mutates and saves the row it is given, so it needs the
        # real one: resolve_visible_wiki hands back a concealed projection to a
        # gated viewer, and saving that would persist their redacted view over
        # what the community actually wrote.
        target = writable_wiki(wiki)
        try:
            # baseline=wiki: the dialog was prefilled from the projection and
            # posts every field, touched or not.
            edit = apply_wiki_edit(target, profile, body, strict=False, baseline=wiki)
        except WikiEditValidationError as exc:
            return JsonResponse({"error": exc.message}, status=400)

        if edit is None:
            return JsonResponse({"ok": True, "message": "No changes detected."})
        changes = edit.changes

        # Description, dates, and security indicators all render together in the
        # "About" card - send back the freshly-rendered fragment so the client can
        # swap it in place instead of leaving edited-but-unrendered fields stale.
        # Concealed again on the way out: this fragment goes back to the viewer
        # who just wrote, and `target` is now carrying everyone's values.
        about_html = render_to_string("dashboard/partials/wiki/_wiki_about_card.html", {"wiki": conceal_wiki(target, profile)}, request=request)
        return JsonResponse({"ok": True, "changes": list(changes.keys()), "about_html": about_html})


def _render_history(request, location: Location, wiki: Wiki):
    """Render the edit-history partial, including the requester's own profile.

    Shared by the history list view and the revert/delete actions below so a
    successful action re-renders the up-to-date list in place, instead of
    leaving a stale row (or a raw JSON body) swapped into the DOM.
    """
    from urbanlens.dashboard.services.wiki.concealment import conceal_rows, conceal_wiki, concealment_active, redact_edit_changes

    profile, _ = Profile.objects.get_or_create(user=request.user)
    edits = wiki.edits.select_related("editor__user", "reverted_by").order_by("-created")

    conceal = concealment_active(wiki, profile)
    if conceal:
        # Two separate problems here. The list itself names every editor and
        # what they changed, so it is filtered to the viewer and their friends.
        # And each surviving row's `changes` still carries the *pre-edit* value
        # in its "from" side - which for the viewer's own edit is whatever a
        # stranger had written there. That half survives a perfect read gate,
        # because it lands in content the rules promise always to show.
        # Materialised before mutating: a queryset re-runs its query on each
        # iteration, so redacting in place and handing the queryset to the
        # template would render the unredacted rows from a second fetch.
        visible_edits = list(conceal_rows(edits, profile))
        for edit in visible_edits:
            edit.changes = redact_edit_changes(edit.changes)
        rows: Any = visible_edits
    else:
        rows = edits

    return render(
        request,
        "dashboard/pages/location/wiki_history.html",
        {"location": location, "wiki": conceal_wiki(wiki, profile), "edits": rows, "current_profile": profile},
    )


class LocationWikiHistoryView(LoginRequiredMixin, View):
    """HTMX partial: edit history list for a wiki.

    GET /location/<slug>/wiki/history/
    """

    def get(self, request, location_slug):
        location, wiki, _profile = resolve_visible_wiki(request, location_slug)
        return _render_history(request, location, wiki)


class LocationWikiRevertView(LoginRequiredMixin, View):
    """Revert a specific WikiEdit.

    POST /location/<slug>/wiki/history/<edit_id>/revert/
    Creates a new WikiEdit that restores the "from" values and marks the
    original edit as reverted.
    """

    def post(self, request, location_slug, edit_id: int):
        from urbanlens.dashboard.services.wiki.concealment import writable_wiki

        location, wiki, profile = resolve_visible_wiki(request, location_slug)
        target_edit = get_object_or_404(visible_rows(WikiEdit.objects.filter(wiki=wiki), wiki, profile), id=edit_id)

        if target_edit.reverted:
            return JsonResponse({"error": "This edit has already been reverted."}, status=400)

        # Reverting writes the "from" values back, so it needs the real row -
        # `wiki` may be a projection carrying this viewer's redacted view.
        target = writable_wiki(wiki)
        revert_edit, skipped_fields = revert_wiki_edit(location, target, profile, target_edit)

        if revert_edit is None:
            # Every field this edit touched was changed again by someone else
            # since - nothing left to revert. Don't record a no-op WikiEdit or
            # mark the original as reverted.
            response = _render_history(request, location, target)
            response["HX-Trigger"] = json.dumps(
                {"showToast": {"level": "warning", "message": f"Could not revert - every field was changed again since this edit: {', '.join(skipped_fields)}."}},
            )
            return response

        response = _render_history(request, location, target)
        if skipped_fields:
            response["HX-Trigger"] = json.dumps(
                {"showToast": {"level": "warning", "message": f"Reverted, but some fields were left unchanged because they were edited again since: {', '.join(skipped_fields)}."}},
            )
        return response


class LocationWikiEditDeleteView(LoginRequiredMixin, View):
    """Permanently erase one of the requesting user's own wiki edits.

    POST /location/<slug>/wiki/history/<edit_id>/delete/

    Unlike a plain revert (which keeps the edit visible in history, just
    flagged as reverted), this restores the fields to their pre-edit values
    (if not already reverted) and hard-deletes the WikiEdit row - and its
    revert record, if any, since that also carries the original value as its
    "from" - so no copy of the data lingers anywhere. Intended for cases like
    accidentally pasting private information into a public wiki field.

    Only the editor who made the original edit may delete it.
    """

    def post(self, request, location_slug, edit_id: int):
        from urbanlens.dashboard.services.wiki.concealment import writable_wiki

        location, wiki, profile = resolve_visible_wiki(request, location_slug)
        target_edit = get_object_or_404(visible_rows(WikiEdit.objects.filter(wiki=wiki), wiki, profile), id=edit_id)

        if target_edit.editor_id != profile.id:
            return JsonResponse({"error": "You can only permanently delete your own edits."}, status=403)

        # Both the revert and the purge below write, so both act on the real
        # row rather than whatever this viewer is entitled to see.
        target = writable_wiki(wiki)

        skipped_fields: list[str] = []
        if not target_edit.reverted:
            revert_changes, skipped_fields = revert_edit_fields(location, target, target_edit)
            save_edited_fields(target, revert_changes)

        # The field-revision log records every write, so the value this view
        # exists to erase also survives there, with the editor's name on it.
        # Purge those rows before dropping the edit, or the promise in this
        # view's docstring stops being true - see
        # models.abstract.versioned.purge_recorded_value.
        from urbanlens.dashboard.models.abstract.versioned import purge_recorded_value

        for field_name, diff in (target_edit.changes or {}).items():
            if isinstance(diff, dict) and "to" in diff:
                purge_recorded_value(target, field_name, diff["to"])

        revert_record = target_edit.reverted_by
        if revert_record is not None:
            revert_record.delete()
        target_edit.delete()

        response = _render_history(request, location, target)
        if skipped_fields:
            response["HX-Trigger"] = json.dumps(
                {"showToast": {"level": "warning", "message": f"Deleted, but some fields were left unchanged because they were edited again since: {', '.join(skipped_fields)}."}},
            )
        return response


class PublicPinVoteView(LoginRequiredMixin, View):
    """Cast, change, or withdraw the requester's public-pin ballot.

    POST /location/<slug>/wiki/public-vote/
    Body: ``choice`` = ``public`` | ``private`` | ``withdraw``.

    Re-renders just the vote block. Ballots are anonymous and no tally is
    ever shown - the response only reflects the requester's own choice.
    """

    def post(self, request, location_slug):
        from urbanlens.dashboard.services.wiki.concealment import concealment_active

        location, wiki, profile = resolve_visible_wiki(request, location_slug)

        try:
            cast_public_vote(location, profile, request.POST.get("choice") or "")
        except PublicVoteError as exc:
            return JsonResponse({"error": exc.safe_message}, status=400)

        return render(
            request,
            "dashboard/partials/pins/_public_pin_vote_block.html",
            {"location": location, "public_vote": public_vote_context(location, profile, conceal=concealment_active(wiki, profile))},
        )


class BoundaryVoteView(LoginRequiredMixin, View):
    """Cast or change the requester's vote for a location's official boundary.

    POST /location/<slug>/wiki/boundary/vote/
    Body: ``boundary_id`` = pk of one of the location's candidate boundaries.

    Returns JSON with the requester's (new) choice and whether the community
    now has consensus - the dialog updates itself client-side rather than
    re-rendering (its mini Leaflet maps would otherwise need re-initializing).
    """

    def post(self, request, location_slug):
        from urbanlens.dashboard.services.wiki.concealment import concealment_active

        location, wiki, profile = resolve_visible_wiki(request, location_slug)

        try:
            boundary_id = int(request.POST.get("boundary_id") or 0)
        except (TypeError, ValueError):
            boundary_id = 0

        try:
            vote = cast_boundary_vote(location.place, profile, boundary_id)
        except BoundaryVoteError as exc:
            return JsonResponse({"error": exc.safe_message}, status=400)

        # Same conceal-aware answer the GET's boundary_vote_context computes -
        # the raw has_consensus() states that other people voted, which is
        # exactly what boundary_vote_context(conceal=True) exists to hide.
        vote_context = boundary_vote_context(location.place, profile, conceal=concealment_active(wiki, profile))
        return JsonResponse(
            {
                "ok": True,
                "my_vote_id": vote.boundary_id,
                "has_consensus": bool(vote_context and vote_context.get("has_consensus")),
            },
        )


class WikiStatVoteView(LoginRequiredMixin, View):
    """Cast or clear the requester's vote on one community stat field.

    POST /location/<slug>/wiki/stat/<field>/vote/
    Body: ``value`` (1-5) to cast a vote, or 0/absent to clear it.

    Re-renders just that stat item - both the recalculated composite and the
    viewer's own vote - never a full page reload.
    """

    def post(self, request, location_slug, field):
        if field not in WikiStatField.values:
            raise Http404
        _location, wiki, profile = resolve_visible_wiki(request, location_slug)

        try:
            value = int(request.POST.get("value") or 0)
        except (TypeError, ValueError):
            value = 0

        if 1 <= value <= 5:
            WikiStatVote.objects.cast(wiki, profile, field, value)
        else:
            WikiStatVote.objects.clear(wiki, profile, field)

        from urbanlens.dashboard.services.wiki.concealment import concealment_active

        return render(
            request,
            "dashboard/partials/pins/_wiki_stat_rating_item.html",
            {"wiki": wiki, **_wiki_stat_context(wiki, field, profile, conceal=concealment_active(wiki, profile))},
        )
