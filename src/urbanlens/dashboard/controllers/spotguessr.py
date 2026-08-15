"""SpotGuessr controller - gameplay, multiplayer lobby, and chat (UL-391..UL-393).

See ``docs/designs/drafts/spotguessr.md`` for the full rules. Session/round/guess/
lobby orchestration lives in ``services.spotguessr`` - this module only
handles HTTP: request parsing, participant/ownership checks, and JSON
serialization (a round's answer is never serialized until a guess reveals
it; real-time fan-out to other participants happens over
``consumers.GameSessionConsumer``, not here).
"""

from __future__ import annotations

from datetime import date as date_cls, timedelta
import json
import logging
from typing import TYPE_CHECKING, Any

from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.gis.gdal.error import GDALException
from django.contrib.gis.geos import GEOSException, Point
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views import View

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse

from urbanlens.dashboard.controllers.games import GAMES, AlphaFeatureRequiredMixin, rating_stats
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.spotguessr.model import (
    GameRound,
    GameSession,
    GameSessionParticipant,
    Guess,
    SpotGuessrMode,
)
from urbanlens.dashboard.services.social.connections import get_connections
from urbanlens.dashboard.services.spotguessr import (
    chat as spotguessr_chat,
    overview as spotguessr_overview,
    relevance as spotguessr_relevance,
    serializers,
    session as spotguessr_session,
)
from urbanlens.dashboard.services.spotguessr.social import visible_friend_ratings
from urbanlens.UrbanLens.settings.app import settings

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


def _parse_geo_bounds(geo_bounds_raw: str | None) -> tuple[dict | None, JsonResponse | None]:
    """Parse+validate an optional ``geo_bounds`` GeoJSON string.

    Returns:
        ``(geojson, None)`` on success, or ``(None, error_response)`` on
        failure - the caller should return ``error_response`` immediately
        when it's not None.
    """
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


def _validate_label_id(raw_label_id: str | None, profile: Profile) -> tuple[int | None, JsonResponse | None]:
    """Parse+validate an optional ``label_id`` - must name a label ``profile`` can actually use.

    Mirrors ``_parse_geo_bounds``'s shape. Scoped to ``Label.objects.location_labels().visible_to(profile)``
    (the same visibility rule the map's label filter uses) so a profile can't probe for the
    existence of another profile's private label via the game-start endpoint - an id that
    doesn't resolve is reported identically whether it's malformed, someone else's personal
    label, or simply doesn't exist.

    Returns:
        ``(label_id, None)`` on success (``label_id`` is None when omitted), or
        ``(None, error_response)``.
    """
    if not raw_label_id:
        return None, None
    try:
        label_id = int(raw_label_id)
    except (TypeError, ValueError):
        return None, JsonResponse({"error": "label_id must be an integer."}, status=400)
    if not Label.objects.location_labels().visible_to(profile).filter(pk=label_id).exists():
        return None, JsonResponse({"error": "Unknown label."}, status=400)
    return label_id, None


def _config_from_request(request: HttpRequest, profile: Profile) -> tuple[spotguessr_session.GameConfig | None, JsonResponse | None]:
    """Build+validate a GameConfig from POST fields, shared by solo and multiplayer start.

    Returns:
        ``(config, None)`` on success, or ``(None, error_response)``.
    """
    geo_bounds_geojson, error = _parse_geo_bounds(request.POST.get("geo_bounds"))
    if error is not None:
        return None, error

    label_id, error = _validate_label_id(request.POST.get("label_id"), profile)
    if error is not None:
        return None, error

    try:
        difficulty = float(request.POST.get("difficulty", 0.5))
    except (TypeError, ValueError):
        return None, JsonResponse({"error": "difficulty must be a number between 0 and 1."}, status=400)

    round_time_limit_seconds = None
    raw_time_limit = request.POST.get("round_time_limit_seconds")
    if raw_time_limit:
        try:
            round_time_limit_seconds = int(raw_time_limit)
        except (TypeError, ValueError):
            return None, JsonResponse({"error": "round_time_limit_seconds must be a whole number of seconds."}, status=400)
        if round_time_limit_seconds not in spotguessr_session.ROUND_TIME_LIMIT_CHOICES:
            return None, JsonResponse({"error": f"round_time_limit_seconds must be one of {spotguessr_session.ROUND_TIME_LIMIT_CHOICES} or omitted."}, status=400)

    config = spotguessr_session.GameConfig(
        difficulty=difficulty,
        allow_arbitrary_external_photos=request.POST.get("allow_arbitrary_external_photos") == "on",
        require_visited_all=request.POST.get("require_visited_all") == "on",
        date_guessing_enabled=request.POST.get("date_guessing_enabled") == "on",
        use_aliases=request.POST.get("use_aliases", "on") == "on",
        geo_bounds_geojson=geo_bounds_geojson,
        round_time_limit_seconds=round_time_limit_seconds,
        label_id=label_id,
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
        "area_pin_count": reverse("spotguessr.area_pin_count"),
        "settings": reverse("spotguessr.settings"),
        "friends": reverse("spotguessr.friends"),
        "lobby": reverse("spotguessr.lobby", kwargs=session_kwargs),
        "invite": reverse("spotguessr.invite", kwargs=session_kwargs),
        "join": reverse("spotguessr.join", kwargs=session_kwargs),
        "begin": reverse("spotguessr.begin", kwargs=session_kwargs),
        "end": reverse("spotguessr.end", kwargs=session_kwargs),
        "round": reverse("spotguessr.round", kwargs=session_kwargs),
        "guess": reverse("spotguessr.guess", kwargs={"session_id": _SESSION_ID_SENTINEL, "round_id": _ROUND_ID_SENTINEL}),
        "round_timeout": reverse("spotguessr.round_timeout", kwargs={"session_id": _SESSION_ID_SENTINEL, "round_id": _ROUND_ID_SENTINEL}),
        "photo_feedback": reverse("spotguessr.photo_feedback", kwargs={"session_id": _SESSION_ID_SENTINEL, "round_id": _ROUND_ID_SENTINEL}),
        "chat_history": reverse("spotguessr.chat_history", kwargs=session_kwargs),
        "summary": reverse("spotguessr.summary", kwargs=session_kwargs),
        "session_id_sentinel": str(_SESSION_ID_SENTINEL),
        "round_id_sentinel": str(_ROUND_ID_SENTINEL),
    }


#: Presentation metadata for the 3 mode-selection cards on the overview page -
#: keyed by SpotGuessrMode value so the template can loop over SpotGuessrMode.choices
#: instead of hardcoding 3 cards (stays correct if a mode is ever added/removed).
_MODE_CARD_META: dict[str, dict[str, str]] = {
    SpotGuessrMode.PHOTOS: {"icon": "photo_camera", "description": "Guess where a photo was taken.", "accent": "photos"},
    SpotGuessrMode.NAMED_PLACE: {"icon": "signpost", "description": "Guess a place from its name or alias.", "accent": "named_place"},
    SpotGuessrMode.STREET_VIEW: {"icon": "streetview", "description": "Guess from a street-level view.", "accent": "street_view"},
}


def _mode_cards() -> list[dict[str, str]]:
    """The 3 mode cards' display data, in ``SpotGuessrMode`` declaration order."""
    return [{"value": value, "label": label, **_MODE_CARD_META[value]} for value, label in SpotGuessrMode.choices]


def _prewarm_solo_start(profile_id: int, mode: str, last_config: dict) -> None:
    """Queue ``tasks.prewarm_spotguessr_solo_start`` - see ``SpotGuessrHomeView.get``'s call site."""
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.tasks import prewarm_spotguessr_solo_start

    safely_enqueue_task(prewarm_spotguessr_solo_start, profile_id, mode, last_config)


class SpotGuessrHomeView(LoginRequiredMixin, AlphaFeatureRequiredMixin, View):
    """The SpotGuessr overview page: own rating, friends' ratings, start-game form.

    GET /spotguessr/
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        profile = _current_profile(request)
        preference = spotguessr_overview.get_preference(profile)
        # Whichever mode the player most recently played, not hardcoded to
        # Photos - a rating for a Named Place/Street View-only player was
        # updating correctly all along, the homepage chip just never looked
        # at the right row (see docs/PROBLEMS.md/git history for the report).
        # Shared with the external API's overview endpoint via
        # ``services.spotguessr.overview`` so the two can't answer differently.
        own_rating = spotguessr_overview.most_recent_rating(profile)

        # An invite notification links here with ?session=<id> (there's no
        # dedicated per-session page - this single-page view holds all
        # client-side session state) - only honored when the profile is
        # actually a participant, same 404-shaped silence as everywhere else.
        initial_session_id = None
        raw_session_id = request.GET.get("session")
        if raw_session_id and GameSessionParticipant.objects.filter(session_id=raw_session_id, profile=profile).exists():
            initial_session_id = raw_session_id

        # Best-effort speculative prewarm of the round a solo player is most
        # likely to start next (see services.spotguessr.prewarm) - skipped
        # when this load is actually resuming a specific session, since
        # they're not about to start a fresh one. Guessing wrong (a
        # different mode/config, or multiplayer instead) just means the
        # prewarm sits unused until it expires - never a correctness issue,
        # only a missed optimization.
        if initial_session_id is None:
            guessed_mode = own_rating.mode if own_rating is not None else SpotGuessrMode.PHOTOS
            _prewarm_solo_start(profile.pk, guessed_mode, preference.last_config)

        friend_ratings = visible_friend_ratings(profile)

        return render(
            request,
            "dashboard/pages/spotguessr/index.html",
            {
                "page_name": "spotguessr",
                # The shared games subnav and hero (partials/games/) read these.
                "games": GAMES,
                "game_stats": rating_stats(own_rating, friend_ratings),
                "own_rating": own_rating,
                "friend_ratings": friend_ratings,
                "show_ratings_to_friends": preference.show_ratings_to_friends,
                "last_config": preference.last_config,
                "min_rounds": spotguessr_session.MIN_ROUNDS_PER_SESSION,
                "max_rounds": spotguessr_session.MAX_ROUNDS_PER_SESSION,
                "default_rounds": spotguessr_session.DEFAULT_ROUNDS_PER_SESSION,
                "round_time_limit_choices": spotguessr_session.ROUND_TIME_LIMIT_CHOICES,
                "mode_cards": _mode_cards(),
                "labels": Label.objects.location_labels().visible_to(profile).ordered(),
                "urls": _url_templates(),
                "my_profile_id": profile.pk,
                "initial_session_id": initial_session_id,
                # Client-side key for the Street View mode's interactive panorama
                # (google.maps.StreetViewPanorama) - the public/browser-restricted
                # key, not the unrestricted server-side one used to fetch imagery.
                "google_maps_api_key": settings.google_public_api_key,
            },
        )


class SpotGuessrSettingsView(LoginRequiredMixin, AlphaFeatureRequiredMixin, View):
    """Update SpotGuessr preferences.

    POST /spotguessr/settings/   body: ``show_ratings_to_friends`` ("on"/"off")
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        profile = _current_profile(request)
        preference = spotguessr_overview.get_preference(profile)
        preference.show_ratings_to_friends = request.POST.get("show_ratings_to_friends") == "on"
        preference.save(update_fields=["show_ratings_to_friends", "updated"])
        return JsonResponse({"show_ratings_to_friends": preference.show_ratings_to_friends})


class SpotGuessrFriendsView(LoginRequiredMixin, AlphaFeatureRequiredMixin, View):
    """The profile's friends, for the multiplayer invite picker.

    GET /spotguessr/friends/
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        profile = _current_profile(request)
        friends = get_connections(profile)
        return JsonResponse({"friends": [{"profile_id": friend.pk, "username": friend.username} for friend in friends]})


class SpotGuessrStartView(LoginRequiredMixin, AlphaFeatureRequiredMixin, View):
    """Start a new session - solo (immediately active) or multiplayer (a lobby to invite friends into).

    POST /spotguessr/start/   body: ``mode``, ``total_rounds``, difficulty/toggle fields,
    optional ``invite_profile_ids`` (repeated) to start a multiplayer lobby instead of solo play.

    A solo start whose config has no eligible locations at all (e.g. the
    profile hasn't pinned anything yet, or a chosen ``geo_bounds`` excludes
    every pin) never creates a ``GameSession`` - it responds with
    ``{"error_code": "no_eligible_locations"}`` instead, so the frontend can
    show a dedicated empty-state screen rather than a fake "game over."
    Multiplayer can't be pre-checked this way (invitees haven't joined yet
    to know their pins) - see ``SpotGuessrBeginView`` for that case.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        profile = _current_profile(request)

        mode = request.POST.get("mode", SpotGuessrMode.PHOTOS)
        if mode not in SpotGuessrMode.values:
            return JsonResponse({"error": f"Unknown mode {mode!r}."}, status=400)

        config, error = _config_from_request(request, profile)
        if config is None:
            # _config_from_request always pairs a None config with a non-None
            # error response - the fallback below never actually fires.
            return error or JsonResponse({"error": "Invalid request."}, status=400)

        try:
            total_rounds = int(request.POST.get("total_rounds", spotguessr_session.DEFAULT_ROUNDS_PER_SESSION))
        except (TypeError, ValueError):
            total_rounds = spotguessr_session.DEFAULT_ROUNDS_PER_SESSION

        invite_ids = [pid for pid in request.POST.getlist("invite_profile_ids") if pid]

        spotguessr_overview.remember_last_config(profile, config)

        if invite_ids:
            invitees = list(Profile.objects.filter(pk__in=invite_ids))
            try:
                game_session = spotguessr_session.start_multiplayer_session(profile, mode, config, invitees, total_rounds=total_rounds)
            except spotguessr_session.SpotGuessrError as exc:
                return JsonResponse({"error": exc.safe_message}, status=400)
            return JsonResponse({"session_id": game_session.pk, "lobby": True, "session": serializers.serialize_session(game_session)})

        # The whole create-then-verify-then-clean-up sequence lives in the
        # service (``start_solo_playthrough``) so the external API runs the
        # identical one rather than a second copy that could drift into leaving
        # unplayable ACTIVE sessions behind.
        try:
            result = spotguessr_session.start_solo_playthrough(profile, mode, config, total_rounds=total_rounds)
        except spotguessr_session.SpotGuessrError as exc:
            return JsonResponse({"error": exc.safe_message}, status=400)

        if result.round is None or result.session is None:
            return JsonResponse({"error_code": "no_eligible_locations"})

        return JsonResponse({"session_id": result.session.pk, "finished": False, "round": serializers.serialize_round(result.round)})


class SpotGuessrLobbyView(LoginRequiredMixin, AlphaFeatureRequiredMixin, View):
    """The lobby's current state: mode, status, and every participant (invited or joined).

    GET /spotguessr/session/<session_id>/lobby/
    """

    def get(self, request: HttpRequest, session_id: int) -> HttpResponse:
        profile = _current_profile(request)
        game_session = _participant_session(profile, session_id)
        return JsonResponse(serializers.serialize_session(game_session))


class SpotGuessrInviteView(LoginRequiredMixin, AlphaFeatureRequiredMixin, View):
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
            return JsonResponse({"error": exc.safe_message}, status=400)
        return JsonResponse({"participant": serializers.serialize_participant(participant)})


class SpotGuessrJoinView(LoginRequiredMixin, AlphaFeatureRequiredMixin, View):
    """Accept an invitation to a lobby.

    POST /spotguessr/session/<session_id>/join/
    """

    def post(self, request: HttpRequest, session_id: int) -> HttpResponse:
        profile = _current_profile(request)
        game_session = _participant_session(profile, session_id)

        try:
            participant = spotguessr_session.join_session(game_session, profile)
        except spotguessr_session.SpotGuessrError as exc:
            return JsonResponse({"error": exc.safe_message}, status=400)
        return JsonResponse({"participant": serializers.serialize_participant(participant)})


class SpotGuessrBeginView(LoginRequiredMixin, AlphaFeatureRequiredMixin, View):
    """Host starts the game: locks the roster and creates round 1.

    POST /spotguessr/session/<session_id>/begin/

    Unlike solo start, the joined roster's combined eligibility can't be
    checked before this point (invitees may not have joined yet). If
    locking the roster reveals there's nothing eligible for this group,
    the response is ``{"finished": false, "no_eligible_locations": true}``
    rather than a completed summary - the session is left ACTIVE (not
    marked COMPLETED) since it never actually played anything.
    """

    def post(self, request: HttpRequest, session_id: int) -> HttpResponse:
        profile = _current_profile(request)
        game_session = _participant_session(profile, session_id)

        try:
            round_ = spotguessr_session.begin_session(game_session, profile)
        except spotguessr_session.SpotGuessrError as exc:
            return JsonResponse({"error": exc.safe_message}, status=400)

        if round_ is None:
            if spotguessr_session.rounds_played(game_session) == 0:
                return JsonResponse({"finished": False, "no_eligible_locations": True})
            spotguessr_session.complete_session(game_session)
            return JsonResponse({"finished": True, "summary": spotguessr_session.session_summary(game_session)})
        return JsonResponse({"finished": False, "round": serializers.serialize_round(round_)})


class SpotGuessrEndSessionView(LoginRequiredMixin, AlphaFeatureRequiredMixin, View):
    """Host ends the game immediately - the manual escape hatch for a stalled or AFK multiplayer session.

    POST /spotguessr/session/<session_id>/end/

    Unlike waiting out the stall-sweep Celery task (``tasks.sweep_stalled_spotguessr_sessions``),
    this lets the host end the game the moment they decide it's not going
    anywhere - see the SpotGuessr audit's "no host ability to end the game"
    finding. Any in-flight round is revealed first with whatever guesses
    already exist (see ``services.spotguessr.session.end_session_now``).
    """

    def post(self, request: HttpRequest, session_id: int) -> HttpResponse:
        profile = _current_profile(request)
        game_session = _participant_session(profile, session_id)

        try:
            spotguessr_session.end_session_now(game_session, profile)
        except spotguessr_session.SpotGuessrError as exc:
            return JsonResponse({"error": exc.safe_message}, status=400)
        return JsonResponse({"finished": True, "summary": spotguessr_session.session_summary(game_session)})


class SpotGuessrRoundView(LoginRequiredMixin, AlphaFeatureRequiredMixin, View):
    """The session's current round (for reloads/reconnects).

    GET /spotguessr/session/<session_id>/round/

    Same ``no_eligible_locations`` distinction as ``SpotGuessrBeginView`` -
    a session that ran out of eligible locations before playing any round
    at all is reported that way instead of as a completed game.
    """

    def get(self, request: HttpRequest, session_id: int) -> HttpResponse:
        profile = _current_profile(request)
        game_session = _participant_session(profile, session_id)

        round_ = spotguessr_session.get_or_create_round(game_session)
        if round_ is None:
            if spotguessr_session.rounds_played(game_session) == 0:
                return JsonResponse({"finished": False, "no_eligible_locations": True})
            spotguessr_session.complete_session(game_session)
            return JsonResponse({"finished": True, "summary": spotguessr_session.session_summary(game_session)})

        return JsonResponse({"finished": False, "round": serializers.serialize_round(round_)})


class SpotGuessrGuessView(LoginRequiredMixin, AlphaFeatureRequiredMixin, View):
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
            guess, bonus_tiers, rating_change = spotguessr_session.submit_guess(round_, profile, guess_point, guessed_date)
        except spotguessr_session.SpotGuessrError as exc:
            return JsonResponse({"error": exc.safe_message}, status=400)

        round_.refresh_from_db()
        return JsonResponse(serializers.serialize_reveal(round_, guess, bonus_tiers, rating_change))


class SpotGuessrRoundTimeoutView(LoginRequiredMixin, AlphaFeatureRequiredMixin, View):
    """Force-reveal the current round because its configured round timer expired.

    POST /spotguessr/session/<session_id>/round/<round_id>/timeout/

    The authoritative check is server-side (``round_.created`` + the
    session's own ``round_time_limit_seconds``, never trusting the client's
    clock) - this just gives a client that's still connected a fast path to
    ``expire_round_timer``, which treats "time's up" as an ordinary round
    outcome rather than a stall (unlike the stall-sweep Celery task's
    ``force_reveal_round``, which is the much slower safety net for a client
    that's disappeared entirely - see both functions' docstrings). A no-op
    (200, not an error) if the round is already revealed or the timer
    genuinely hasn't expired yet - a late/duplicate/clock-skewed call is
    harmless either way.
    """

    def post(self, request: HttpRequest, session_id: int, round_id: int) -> HttpResponse:
        profile = _current_profile(request)
        game_session = _participant_session(profile, session_id)
        _joined_participant(profile, game_session)

        round_ = get_object_or_404(GameRound, pk=round_id, session=game_session)
        time_limit = (game_session.config or {}).get("round_time_limit_seconds")
        if round_.revealed_at is None and time_limit and timezone.now() >= round_.created + timedelta(seconds=time_limit):
            spotguessr_session.expire_round_timer(round_)
            round_.refresh_from_db()

        return JsonResponse({"revealed": round_.revealed_at is not None})


class SpotGuessrPhotoFeedbackView(LoginRequiredMixin, AlphaFeatureRequiredMixin, View):
    """Thumbs up/down, or report, the photo a Photos-mode round just showed.

    POST /spotguessr/session/<session_id>/round/<round_id>/feedback/   body: ``kind`` (thumbs_up/thumbs_down/reported)

    Feeds ``services.media.media_relevance.effective_relevance`` at a reduced
    weight (or, for a report, full weight against "not relevant") - see
    ``services.spotguessr.relevance`` for exactly how. A no-op for a round
    with no photo (Named Place/Street View) or one this profile never
    guessed on - there's no "reaction to a photo you weren't shown" case to
    support.
    """

    def post(self, request: HttpRequest, session_id: int, round_id: int) -> HttpResponse:
        profile = _current_profile(request)
        game_session = _participant_session(profile, session_id)
        participant = _joined_participant(profile, game_session)
        if not participant.is_joined:
            return JsonResponse({"error": "Accept the invite before playing."}, status=403)

        round_ = get_object_or_404(GameRound, pk=round_id, session=game_session)
        kind = request.POST.get("kind")
        if kind not in spotguessr_relevance.EXPLICIT_KINDS:
            return JsonResponse({"error": f"kind must be one of {', '.join(spotguessr_relevance.EXPLICIT_KINDS)}."}, status=400)
        if not Guess.objects.filter(round=round_, profile=profile).exists():
            return JsonResponse({"error": "You can only react to a round you've guessed on."}, status=403)

        feedback = spotguessr_relevance.record_feedback(round_, profile, kind)
        if feedback is None:
            return JsonResponse({"error": "This round has no photo to react to."}, status=400)
        return JsonResponse({"kind": feedback.kind})


class SpotGuessrChatHistoryView(LoginRequiredMixin, AlphaFeatureRequiredMixin, View):
    """Recent chat messages, for reconnects/late page-opens - live messages arrive over the WebSocket.

    GET /spotguessr/session/<session_id>/chat/
    """

    def get(self, request: HttpRequest, session_id: int) -> HttpResponse:
        profile = _current_profile(request)
        game_session = _participant_session(profile, session_id)
        messages = spotguessr_chat.recent_messages(game_session)
        return JsonResponse({"messages": [serializers.serialize_chat_message(message) for message in messages]})


class SpotGuessrPinsView(LoginRequiredMixin, AlphaFeatureRequiredMixin, View):
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
        # location__wiki as well: the label falls through to Location.display_name,
        # which reads the reverse OneToOne `wiki` - one query per pin without it.
        pins = Pin.objects.filter(profile=profile).select_related("location", "location__wiki")
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


class SpotGuessrAreaPinCountView(LoginRequiredMixin, AlphaFeatureRequiredMixin, View):
    """How many of the profile's own pins fall inside a candidate ``geo_bounds`` selection.

    GET /spotguessr/area_pin_count/?geo_bounds=<geojson>

    Lets the settings dialog show "N of your pins are in this area" the
    moment a region is drawn/searched, instead of leaving the player to
    guess whether their chosen area contains anything playable.
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        profile = _current_profile(request)
        geo_bounds_geojson, error = _parse_geo_bounds(request.GET.get("geo_bounds"))
        if error is not None:
            return error
        if geo_bounds_geojson is None:
            return JsonResponse({"error": "geo_bounds is required."}, status=400)

        geo_bounds = spotguessr_session.GameConfig(geo_bounds_geojson=geo_bounds_geojson).geo_bounds
        count = Pin.objects.filter(profile=profile, location__point__within=geo_bounds).count()
        return JsonResponse({"count": count})


class SpotGuessrSummaryView(LoginRequiredMixin, AlphaFeatureRequiredMixin, View):
    """The session's final scoreboard.

    GET /spotguessr/session/<session_id>/summary/
    """

    def get(self, request: HttpRequest, session_id: int) -> HttpResponse:
        profile = _current_profile(request)
        game_session = _participant_session(profile, session_id)
        return JsonResponse(spotguessr_session.session_summary(game_session))
