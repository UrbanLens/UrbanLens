"""SpotGuessr controller - solo Photos-mode gameplay (UL-391).

See ``docs/designs/spotguessr.md`` for the full rules. Session/round/guess
orchestration lives in ``services.spotguessr.session`` - this module only
handles HTTP: request parsing, participant/ownership checks, and JSON
serialization (a round's answer is never serialized until a guess reveals it).
"""

from __future__ import annotations

from datetime import date as date_cls
import json
import logging
from typing import TYPE_CHECKING, Any

from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.gis.geos import GEOSException, Point
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views import View

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse

from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.spotguessr.model import (
    GameRound,
    GameSession,
    GameSessionParticipant,
    PlayerModeRating,
    SpotGuessrMode,
    SpotGuessrPreference,
)
from urbanlens.dashboard.services.spotguessr import session as spotguessr_session
from urbanlens.dashboard.services.spotguessr.social import visible_friend_ratings

logger = logging.getLogger(__name__)


def _current_profile(request: HttpRequest) -> Profile:
    profile, _ = Profile.objects.get_or_create(user=request.user)
    return profile


def _participant_session(profile: Profile, session_id: int) -> GameSession:
    """The session, only if ``profile`` actually participates in it - 404 otherwise.

    404 (not 403) mirrors the boundary-vote endpoint's convention: a session
    another profile is playing shouldn't even reveal that it exists.
    """
    participant = GameSessionParticipant.objects.filter(session_id=session_id, profile=profile).select_related("session").first()
    if participant is None:
        raise Http404("No such session for this profile.")
    return participant.session


def _serialize_round(round_: GameRound) -> dict[str, Any]:
    """Round data safe to send before it's guessed - never the answer."""
    data: dict[str, Any] = {
        "round_id": round_.pk,
        "session_id": round_.session_id,
        "sequence_index": round_.sequence_index,
        "revealed": round_.revealed_at is not None,
    }
    if round_.image_id and round_.image is not None:
        data["image_url"] = round_.image.image.url
        data["image_caption"] = round_.image.caption
    return data


def _serialize_reveal(round_: GameRound, guess) -> dict[str, Any]:
    """The answer + this guess's score - only ever returned after a guess is recorded."""
    location = round_.location
    return {
        "distance_meters": guess.distance_meters,
        "points": guess.points,
        "date_points": guess.date_points,
        "actual_latitude": float(location.latitude),
        "actual_longitude": float(location.longitude),
        "location_name": location.official_name,
    }


class SpotGuessrHomeView(LoginRequiredMixin, View):
    """The SpotGuessr overview page: own rating, friends' ratings, start-game form.

    GET /spotguessr/
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        profile = _current_profile(request)
        preference, _ = SpotGuessrPreference.objects.get_or_create(profile=profile)
        own_rating = PlayerModeRating.objects.filter(profile=profile, mode=SpotGuessrMode.PHOTOS).first()
        return render(
            request,
            "dashboard/pages/spotguessr/index.html",
            {
                "page_name": "spotguessr",
                "own_rating": own_rating,
                "friend_ratings": visible_friend_ratings(profile, SpotGuessrMode.PHOTOS),
                "show_ratings_to_friends": preference.show_ratings_to_friends,
                "last_config": preference.last_config,
                "min_rounds": spotguessr_session.MIN_ROUNDS_PER_SESSION,
                "max_rounds": spotguessr_session.MAX_ROUNDS_PER_SESSION,
                "default_rounds": spotguessr_session.DEFAULT_ROUNDS_PER_SESSION,
            },
        )


class SpotGuessrSettingsView(LoginRequiredMixin, View):
    """Update SpotGuessr preferences.

    POST /spotguessr/settings/   body: ``show_ratings_to_friends`` ("on"/"off")
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        profile = _current_profile(request)
        preference, _ = SpotGuessrPreference.objects.get_or_create(profile=profile)
        preference.show_ratings_to_friends = request.POST.get("show_ratings_to_friends") == "on"
        preference.save(update_fields=["show_ratings_to_friends", "updated"])
        return JsonResponse({"show_ratings_to_friends": preference.show_ratings_to_friends})


class SpotGuessrStartView(LoginRequiredMixin, View):
    """Start a new solo session and return its first round.

    POST /spotguessr/start/
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        profile = _current_profile(request)

        geo_bounds_raw = request.POST.get("geo_bounds")
        try:
            geo_bounds_geojson = json.loads(geo_bounds_raw) if geo_bounds_raw else None
        except (TypeError, ValueError):
            return JsonResponse({"error": "Invalid geo_bounds - must be GeoJSON."}, status=400)

        try:
            difficulty = float(request.POST.get("difficulty", 0.5))
        except (TypeError, ValueError):
            return JsonResponse({"error": "difficulty must be a number between 0 and 1."}, status=400)

        config = spotguessr_session.GameConfig(
            difficulty=difficulty,
            external_media_only=request.POST.get("external_media_only") == "on",
            require_visited_all=request.POST.get("require_visited_all") == "on",
            date_guessing_enabled=request.POST.get("date_guessing_enabled") == "on",
            geo_bounds_geojson=geo_bounds_geojson,
        )
        try:
            # GameConfig.geo_bounds only parses the GeoJSON lazily on access -
            # force it now so a malformed-but-valid-JSON payload 400s here,
            # rather than surfacing as a 500 later inside round generation.
            _ = config.geo_bounds
        except (GEOSException, ValueError, TypeError):
            return JsonResponse({"error": "Invalid geo_bounds - must be a valid GeoJSON polygon."}, status=400)

        try:
            total_rounds = int(request.POST.get("total_rounds", spotguessr_session.DEFAULT_ROUNDS_PER_SESSION))
        except (TypeError, ValueError):
            total_rounds = spotguessr_session.DEFAULT_ROUNDS_PER_SESSION

        try:
            game_session = spotguessr_session.start_solo_session(profile, SpotGuessrMode.PHOTOS, config, total_rounds=total_rounds)
        except spotguessr_session.SpotGuessrError as exc:
            return JsonResponse({"error": str(exc)}, status=400)

        preference, _ = SpotGuessrPreference.objects.get_or_create(profile=profile)
        preference.last_config = config.to_dict()
        preference.save(update_fields=["last_config", "updated"])

        round_ = spotguessr_session.get_or_create_round(game_session)
        if round_ is None:
            spotguessr_session.complete_session(game_session)
            return JsonResponse({"session_id": game_session.pk, "finished": True, "summary": spotguessr_session.session_summary(game_session)})

        return JsonResponse({"session_id": game_session.pk, "finished": False, "round": _serialize_round(round_)})


class SpotGuessrRoundView(LoginRequiredMixin, View):
    """The session's current round (for reloads/reconnects).

    GET /spotguessr/session/<session_id>/round/
    """

    def get(self, request: HttpRequest, session_id: int) -> HttpResponse:
        profile = _current_profile(request)
        game_session = _participant_session(profile, session_id)

        round_ = spotguessr_session.get_or_create_round(game_session)
        if round_ is None:
            spotguessr_session.complete_session(game_session)
            return JsonResponse({"finished": True, "summary": spotguessr_session.session_summary(game_session)})

        return JsonResponse({"finished": False, "round": _serialize_round(round_)})


class SpotGuessrGuessView(LoginRequiredMixin, View):
    """Submit a guess for the session's current round.

    POST /spotguessr/session/<session_id>/round/<round_id>/guess/   body: ``latitude``, ``longitude``, optional ``guessed_date`` (YYYY-MM-DD)
    """

    def post(self, request: HttpRequest, session_id: int, round_id: int) -> HttpResponse:
        profile = _current_profile(request)
        game_session = _participant_session(profile, session_id)
        round_ = get_object_or_404(GameRound, pk=round_id, session=game_session)

        try:
            latitude = float(request.POST["latitude"])
            longitude = float(request.POST["longitude"])
        except (KeyError, TypeError, ValueError):
            return JsonResponse({"error": "latitude and longitude are required."}, status=400)

        guessed_date = None
        if raw_date := request.POST.get("guessed_date"):
            try:
                guessed_date = date_cls.fromisoformat(raw_date)
            except ValueError:
                return JsonResponse({"error": "guessed_date must be YYYY-MM-DD."}, status=400)

        guess_point = Point(longitude, latitude, srid=4326)
        try:
            guess = spotguessr_session.submit_guess(round_, profile, guess_point, guessed_date)
        except spotguessr_session.SpotGuessrError as exc:
            return JsonResponse({"error": str(exc)}, status=400)

        round_.refresh_from_db()
        return JsonResponse(_serialize_reveal(round_, guess))


class SpotGuessrPinsView(LoginRequiredMixin, View):
    """The profile's own pins, for the "search my pins to guess" input.

    GET /spotguessr/pins/

    Solo play's eligibility is exactly "the player's own pinned locations"
    (see ``services.spotguessr.eligibility``), so this is simply every pin
    the profile has - no separate query needed to match what a round could
    possibly be.
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        profile = _current_profile(request)
        pins = Pin.objects.filter(profile=profile).select_related("location")
        return JsonResponse(
            {
                "pins": [
                    {
                        "label": pin.get_unique_search_name() or pin.name or "Unnamed pin",
                        "latitude": pin.effective_latitude,
                        "longitude": pin.effective_longitude,
                    }
                    for pin in pins
                    if pin.effective_latitude is not None and pin.effective_longitude is not None
                ],
            },
        )


class SpotGuessrSummaryView(LoginRequiredMixin, View):
    """The session's final scoreboard.

    GET /spotguessr/session/<session_id>/summary/
    """

    def get(self, request: HttpRequest, session_id: int) -> HttpResponse:
        profile = _current_profile(request)
        game_session = _participant_session(profile, session_id)
        return JsonResponse(spotguessr_session.session_summary(game_session))
