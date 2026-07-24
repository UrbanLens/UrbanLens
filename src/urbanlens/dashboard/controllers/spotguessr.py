"""SpotGuessr controller - gameplay, multiplayer lobby, and chat (UL-391..UL-393).

See ``docs/designs/spotguessr.md`` for the full rules. Session/round/guess/
lobby orchestration lives in ``services.spotguessr`` - this module only
handles HTTP: request parsing, participant/ownership checks, and JSON
serialization (a round's answer is never serialized until a guess reveals
it; real-time fan-out to other participants happens over
``consumers.GameSessionConsumer``, not here).
"""

from __future__ import annotations

from datetime import date as date_cls
import json
import logging
from typing import TYPE_CHECKING, Any

from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.gis.gdal.error import GDALException
from django.contrib.gis.geos import GEOSException, Point
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
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
from urbanlens.dashboard.services.connections import get_connections
from urbanlens.dashboard.services.spotguessr import chat as spotguessr_chat, serializers, session as spotguessr_session
from urbanlens.dashboard.services.spotguessr.social import visible_friend_ratings

logger = logging.getLogger(__name__)


def _current_profile(request: HttpRequest) -> Profile:
    profile, _ = Profile.objects.get_or_create(user=request.user)
    return profile


def _participant_session(profile: Profile, session_id: int) -> GameSession:
    """The session, only if ``profile`` participates in it (any status) - 404 otherwise.

    404 (not 403) mirrors the boundary-vote endpoint's convention: a session
    another profile is playing shouldn't even reveal that it exists. Any
    status (INVITED or JOINED) can view - an invitee should see the lobby
    fill up before deciding to accept.
    """
    participant = GameSessionParticipant.objects.filter(session_id=session_id, profile=profile).select_related("session").first()
    if participant is None:
        raise Http404("No such session for this profile.")
    return participant.session


def _joined_participant(profile: Profile, session: GameSession) -> GameSessionParticipant:
    """The profile's participant row, only if they've actually joined (not just been invited).

    Raises:
        Http404: if there's no participant row at all (shouldn't happen if
            called after ``_participant_session``).
    """
    participant = GameSessionParticipant.objects.filter(session=session, profile=profile).first()
    if participant is None:
        raise Http404("No such session for this profile.")
    return participant


def _parse_geo_bounds(request: HttpRequest) -> tuple[dict | None, JsonResponse | None]:
    """Parse+validate the optional ``geo_bounds`` GeoJSON field.

    Returns:
        ``(geojson, None)`` on success, or ``(None, error_response)`` on
        failure - the caller should return ``error_response`` immediately
        when it's not None.
    """
    geo_bounds_raw = request.POST.get("geo_bounds")
    try:
        geo_bounds_geojson = json.loads(geo_bounds_raw) if geo_bounds_raw else None
    except (TypeError, ValueError):
        return None, JsonResponse({"error": "Invalid geo_bounds - must be GeoJSON."}, status=400)

    config = spotguessr_session.GameConfig(geo_bounds_geojson=geo_bounds_geojson)
    try:
        # GameConfig.geo_bounds only parses the GeoJSON lazily on access -
        # force it now so a malformed-but-valid-JSON payload 400s here,
        # rather than surfacing as a 500 later inside round generation.
        _ = config.geo_bounds
    except (GEOSException, GDALException, ValueError, TypeError):
        return None, JsonResponse({"error": "Invalid geo_bounds - must be a valid GeoJSON polygon."}, status=400)
    return geo_bounds_geojson, None


def _config_from_request(request: HttpRequest) -> tuple[spotguessr_session.GameConfig | None, JsonResponse | None]:
    """Build+validate a GameConfig from POST fields, shared by solo and multiplayer start.

    Returns:
        ``(config, None)`` on success, or ``(None, error_response)``.
    """
    geo_bounds_geojson, error = _parse_geo_bounds(request)
    if error is not None:
        return None, error

    try:
        difficulty = float(request.POST.get("difficulty", 0.5))
    except (TypeError, ValueError):
        return None, JsonResponse({"error": "difficulty must be a number between 0 and 1."}, status=400)

    config = spotguessr_session.GameConfig(
        difficulty=difficulty,
        external_media_only=request.POST.get("external_media_only") == "on",
        require_visited_all=request.POST.get("require_visited_all") == "on",
        date_guessing_enabled=request.POST.get("date_guessing_enabled") == "on",
        use_aliases=request.POST.get("use_aliases", "on") == "on",
        geo_bounds_geojson=geo_bounds_geojson,
    )
    return config, None


#: Placeholder ids reversed into the URL templates handed to the frontend -
#: large and distinctive enough that a literal-string ``.replace()`` in JS
#: is unambiguous, since it always runs against a string this function
#: itself produced, never against arbitrary data.
_SESSION_ID_SENTINEL = 999999999
_ROUND_ID_SENTINEL = 888888888


def _url_templates() -> dict[str, str]:
    """Every SpotGuessr endpoint the frontend needs, with numeric-id placeholders for the parameterized ones.

    ``{% url %}`` can't emit a template directly (its ``<int:...>`` path
    converters require a real integer) - reversing with sentinel ids here
    and letting the frontend substitute them is the simplest exact
    alternative. (A prior version of the frontend hardcoded these paths
    without the ``/dashboard/`` prefix the app is actually mounted under -
    this is also the fix for that.)
    """
    session_kwargs = {"session_id": _SESSION_ID_SENTINEL}
    return {
        "start": reverse("spotguessr.start"),
        "pins": reverse("spotguessr.pins"),
        "settings": reverse("spotguessr.settings"),
        "friends": reverse("spotguessr.friends"),
        "lobby": reverse("spotguessr.lobby", kwargs=session_kwargs),
        "invite": reverse("spotguessr.invite", kwargs=session_kwargs),
        "join": reverse("spotguessr.join", kwargs=session_kwargs),
        "begin": reverse("spotguessr.begin", kwargs=session_kwargs),
        "round": reverse("spotguessr.round", kwargs=session_kwargs),
        "guess": reverse("spotguessr.guess", kwargs={"session_id": _SESSION_ID_SENTINEL, "round_id": _ROUND_ID_SENTINEL}),
        "chat_history": reverse("spotguessr.chat_history", kwargs=session_kwargs),
        "summary": reverse("spotguessr.summary", kwargs=session_kwargs),
        "session_id_sentinel": str(_SESSION_ID_SENTINEL),
        "round_id_sentinel": str(_ROUND_ID_SENTINEL),
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
                "modes": SpotGuessrMode.choices,
                "urls": _url_templates(),
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


class SpotGuessrFriendsView(LoginRequiredMixin, View):
    """The profile's friends, for the multiplayer invite picker.

    GET /spotguessr/friends/
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        profile = _current_profile(request)
        friends = get_connections(profile)
        return JsonResponse({"friends": [{"profile_id": friend.pk, "username": friend.username} for friend in friends]})


class SpotGuessrStartView(LoginRequiredMixin, View):
    """Start a new session - solo (immediately active) or multiplayer (a lobby to invite friends into).

    POST /spotguessr/start/   body: ``mode``, ``total_rounds``, difficulty/toggle fields,
    optional ``invite_profile_ids`` (repeated) to start a multiplayer lobby instead of solo play.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        profile = _current_profile(request)

        mode = request.POST.get("mode", SpotGuessrMode.PHOTOS)
        if mode not in SpotGuessrMode.values:
            return JsonResponse({"error": f"Unknown mode {mode!r}."}, status=400)

        config, error = _config_from_request(request)
        if config is None:
            # _config_from_request always pairs a None config with a non-None
            # error response - the fallback below never actually fires.
            return error or JsonResponse({"error": "Invalid request."}, status=400)

        try:
            total_rounds = int(request.POST.get("total_rounds", spotguessr_session.DEFAULT_ROUNDS_PER_SESSION))
        except (TypeError, ValueError):
            total_rounds = spotguessr_session.DEFAULT_ROUNDS_PER_SESSION

        invite_ids = [pid for pid in request.POST.getlist("invite_profile_ids") if pid]

        preference, _ = SpotGuessrPreference.objects.get_or_create(profile=profile)
        preference.last_config = config.to_dict()
        preference.save(update_fields=["last_config", "updated"])

        if invite_ids:
            invitees = list(Profile.objects.filter(pk__in=invite_ids))
            try:
                game_session = spotguessr_session.start_multiplayer_session(profile, mode, config, invitees, total_rounds=total_rounds)
            except spotguessr_session.SpotGuessrError as exc:
                return JsonResponse({"error": str(exc)}, status=400)
            return JsonResponse({"session_id": game_session.pk, "lobby": True, "session": serializers.serialize_session(game_session)})

        try:
            game_session = spotguessr_session.start_solo_session(profile, mode, config, total_rounds=total_rounds)
        except spotguessr_session.SpotGuessrError as exc:
            return JsonResponse({"error": str(exc)}, status=400)

        round_ = spotguessr_session.get_or_create_round(game_session)
        if round_ is None:
            spotguessr_session.complete_session(game_session)
            return JsonResponse({"session_id": game_session.pk, "finished": True, "summary": spotguessr_session.session_summary(game_session)})

        return JsonResponse({"session_id": game_session.pk, "finished": False, "round": serializers.serialize_round(round_)})


class SpotGuessrLobbyView(LoginRequiredMixin, View):
    """The lobby's current state: mode, status, and every participant (invited or joined).

    GET /spotguessr/session/<session_id>/lobby/
    """

    def get(self, request: HttpRequest, session_id: int) -> HttpResponse:
        profile = _current_profile(request)
        game_session = _participant_session(profile, session_id)
        return JsonResponse(serializers.serialize_session(game_session))


class SpotGuessrInviteView(LoginRequiredMixin, View):
    """Invite one more friend to a still-open lobby. Host-only.

    POST /spotguessr/session/<session_id>/invite/   body: ``profile_id``
    """

    def post(self, request: HttpRequest, session_id: int) -> HttpResponse:
        profile = _current_profile(request)
        game_session = _participant_session(profile, session_id)

        try:
            invitee = Profile.objects.get(pk=request.POST.get("profile_id"))
        except (Profile.DoesNotExist, ValueError, TypeError):
            return JsonResponse({"error": "profile_id is required."}, status=400)

        try:
            participant = spotguessr_session.invite_to_session(game_session, profile, invitee)
        except spotguessr_session.SpotGuessrError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        return JsonResponse({"participant": serializers.serialize_participant(participant)})


class SpotGuessrJoinView(LoginRequiredMixin, View):
    """Accept an invitation to a lobby.

    POST /spotguessr/session/<session_id>/join/
    """

    def post(self, request: HttpRequest, session_id: int) -> HttpResponse:
        profile = _current_profile(request)
        game_session = _participant_session(profile, session_id)

        try:
            participant = spotguessr_session.join_session(game_session, profile)
        except spotguessr_session.SpotGuessrError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        return JsonResponse({"participant": serializers.serialize_participant(participant)})


class SpotGuessrBeginView(LoginRequiredMixin, View):
    """Host starts the game: locks the roster and creates round 1.

    POST /spotguessr/session/<session_id>/begin/
    """

    def post(self, request: HttpRequest, session_id: int) -> HttpResponse:
        profile = _current_profile(request)
        game_session = _participant_session(profile, session_id)

        try:
            round_ = spotguessr_session.begin_session(game_session, profile)
        except spotguessr_session.SpotGuessrError as exc:
            return JsonResponse({"error": str(exc)}, status=400)

        if round_ is None:
            spotguessr_session.complete_session(game_session)
            return JsonResponse({"finished": True, "summary": spotguessr_session.session_summary(game_session)})
        return JsonResponse({"finished": False, "round": serializers.serialize_round(round_)})


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

        return JsonResponse({"finished": False, "round": serializers.serialize_round(round_)})


class SpotGuessrGuessView(LoginRequiredMixin, View):
    """Submit a guess for the session's current round.

    POST /spotguessr/session/<session_id>/round/<round_id>/guess/   body: ``latitude``, ``longitude``, optional ``guessed_date`` (YYYY-MM-DD)
    """

    def post(self, request: HttpRequest, session_id: int, round_id: int) -> HttpResponse:
        profile = _current_profile(request)
        game_session = _participant_session(profile, session_id)
        participant = _joined_participant(profile, game_session)
        if not participant.is_joined:
            return JsonResponse({"error": "Accept the invite before playing."}, status=403)

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
        return JsonResponse(serializers.serialize_reveal(round_, guess))


class SpotGuessrChatHistoryView(LoginRequiredMixin, View):
    """Recent chat messages, for reconnects/late page-opens - live messages arrive over the WebSocket.

    GET /spotguessr/session/<session_id>/chat/
    """

    def get(self, request: HttpRequest, session_id: int) -> HttpResponse:
        profile = _current_profile(request)
        game_session = _participant_session(profile, session_id)
        messages = spotguessr_chat.recent_messages(game_session)
        return JsonResponse({"messages": [serializers.serialize_chat_message(message) for message in messages]})


class SpotGuessrPinsView(LoginRequiredMixin, View):
    """The profile's own pins, for the "search my pins to guess" input.

    GET /spotguessr/pins/

    Solo play's eligibility is exactly "the player's own pinned locations"
    (see ``services.spotguessr.eligibility``), so this is simply every pin
    the profile has - no separate query needed to match what a round could
    possibly be. (Named Place mode never shows this input at all - see the
    frontend - but the endpoint itself doesn't need to know that.)
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
