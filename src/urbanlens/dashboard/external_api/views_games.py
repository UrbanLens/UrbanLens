"""External-API views for SpotGuessr: solo play only, over a credential.

The web game is a superset of this: it also has a lobby, friend invites, a
join/begin handshake, host-side "end the game now", and live session chat, all
of which are coordinated over ``consumers.GameSessionConsumer``. **None of that
is mirrored here, and that is a design decision rather than an unfinished
one.** A multiplayer round withholds its reveal until every joined participant
has guessed, and then delivers it as a WebSocket broadcast; a client that
guessed into such a round over HTTP has nothing left to poll and would sit
there forever, looking to its user like the game had hung. So every
session-scoped view below runs :class:`SoloSessionOnlyMixin` and refuses a
multi-participant or still-in-lobby session with a 409 that says, in as many
words, to use the web client.

Everything else defers to ``services.spotguessr``. Nothing here re-derives a
score, a rating, a round, or an eligibility rule - a second implementation of
any of those would not merely duplicate code, it would produce a *different
game* for mobile players than for web ones, which no test on either side would
catch.

Two boundaries are worth stating explicitly, because both are easy to
accidentally remove:

- **404, never 403, for a session the caller does not participate in.** Session
  ids are sequential global integers (``GameSession`` extends
  ``DashboardModel``, which has no uuid), so a 403 would let anyone enumerate
  exactly how many games the site has played and when.
- **Profiles are identified by slug on the wire.** The internal summary payload
  is keyed by profile primary key; those are remapped here before anything is
  sent.
"""

from __future__ import annotations

from datetime import timedelta
import json
import logging
from typing import TYPE_CHECKING, Any, ClassVar

from django.contrib.gis.gdal.error import GDALException
from django.contrib.gis.geos import GEOSException
from django.http import HttpResponse
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework.response import Response

from urbanlens.dashboard.external_api.pagination import PaginatedListMixin
from urbanlens.dashboard.external_api.permissions import credential_grants
from urbanlens.dashboard.external_api.serializers import ErrorSerializer
from urbanlens.dashboard.external_api.serializers_games import (
    SpotGuessrEligibleCountQuerySerializer,
    SpotGuessrEligibleCountResponseSerializer,
    SpotGuessrEligiblePinSerializer,
    SpotGuessrEligiblePinsQuerySerializer,
    SpotGuessrFeedbackResponseSerializer,
    SpotGuessrFeedbackSerializer,
    SpotGuessrGuessResponseSerializer,
    SpotGuessrGuessSerializer,
    SpotGuessrOverviewSerializer,
    SpotGuessrPreferencesResponseSerializer,
    SpotGuessrPreferencesUpdateSerializer,
    SpotGuessrRoundExpireResponseSerializer,
    SpotGuessrRoundResponseSerializer,
    SpotGuessrSessionCreateResponseSerializer,
    SpotGuessrSessionCreateSerializer,
    SpotGuessrSessionListQuerySerializer,
    SpotGuessrSessionSerializer,
    SpotGuessrSummarySerializer,
    build_reveal_payload,
    build_round_payload,
    build_session_payload,
)
from urbanlens.dashboard.external_api.throttling import (
    TIER_WRITE,
    ExternalApiBurstThrottle,
    ExternalApiMediaThrottle,
    ExternalApiReadThrottle,
    ExternalApiWriteThrottle,
    GameStartThrottle,
)
from urbanlens.dashboard.external_api.views import ExternalApiView
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.spotguessr.model import (
    GameRound,
    GameSession,
    GameSessionParticipant,
    GameSessionStatus,
    Guess,
    SpotGuessrMode,
)
from urbanlens.dashboard.services.identity_visibility import resolve_visible_identity
from urbanlens.dashboard.services.spotguessr import (
    overview as spotguessr_overview,
    relevance as spotguessr_relevance,
    round_image as spotguessr_round_image,
    session as spotguessr_session,
)
from urbanlens.dashboard.services.spotguessr.social import visible_friend_ratings

if TYPE_CHECKING:
    from rest_framework.request import Request

    from urbanlens.dashboard.models.spotguessr.model import PlayerModeRating

logger = logging.getLogger(__name__)

#: The body every session-scoped endpoint returns for a session this API will
#: not play. ``error_code`` is machine-readable on purpose: a client needs to
#: branch to "open this in the browser" rather than surface the prose.
MULTIPLAYER_REFUSAL = {"error": "This session requires the web client.", "error_code": "multiplayer_unsupported"}


def _rating_payload(rating: PlayerModeRating | None) -> dict[str, Any] | None:
    """Describe one Glicko-2 rating row on the display scale, or None.

    Args:
        rating: The rating row, or None for a player who has never played.

    Returns:
        The wire payload, or None. ``rating``/``rating_deviation`` are the
        Elo-familiar display values, never the paper's internal mu/phi - no
        client should have to know the scale constant.
    """
    if rating is None:
        return None
    return {
        "mode": rating.mode,
        "rating": round(rating.rating, 1),
        "rating_deviation": round(rating.rating_deviation, 1),
        "games_played": rating.games_played,
        "last_played_at": rating.last_played_at,
    }


def _parse_geo_bounds_query(raw: str | None) -> tuple[dict | None, Response | None]:
    """Parse+validate an optional ``geo_bounds`` GeoJSON query-string value.

    Mirrors ``controllers.spotguessr._parse_geo_bounds`` exactly, translated to
    this API's ``{"error": ...}`` envelope instead of ``JsonResponse``.

    Args:
        raw: The raw ``geo_bounds`` query parameter, or None.

    Returns:
        ``(geojson, None)`` on success (``geojson`` is None when ``raw`` was
        empty), or ``(None, response)`` carrying the 400 to return as-is.
    """
    try:
        geo_bounds_geojson = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None, Response({"error": "Invalid geo_bounds - must be GeoJSON."}, status=400)

    config = spotguessr_session.GameConfig(geo_bounds_geojson=geo_bounds_geojson)
    try:
        # GameConfig.geo_bounds only parses the GeoJSON lazily on access -
        # force it now so a malformed-but-valid-JSON payload 400s here,
        # rather than surfacing as a 500 later inside the pin query.
        _ = config.geo_bounds
    except (GEOSException, GDALException, ValueError, TypeError):
        return None, Response({"error": "Invalid geo_bounds - must be a valid GeoJSON polygon."}, status=400)
    return geo_bounds_geojson, None


class SoloSessionOnlyMixin:
    """Refuses any session this API cannot honestly finish.

    Applied to every session-scoped view. The check is deliberately *not*
    "status != LOBBY": a multiplayer game whose host has already begun it is
    ACTIVE, exactly like a solo one, and it is the one that actually traps a
    client. Both conditions are refused:

    - **more than one participant** - reveals are withheld until every joined
      participant has guessed and then arrive only over the WebSocket, so an
      HTTP client that guesses into such a round has nothing to poll and will
      wait forever;
    - **still in LOBBY** - there is no join/begin handshake on this surface, so
      the session can never leave that state through anything here.

    This is not defensive padding. Without it the failure is silent and
    indistinguishable from a slow server, which is the worst way for a mobile
    client to discover an unsupported feature.
    """

    def refuse_if_multiplayer(self, session: GameSession) -> Response | None:
        """Return the 409 refusal when *session* is not solo-playable, else None.

        Args:
            session: The session the caller asked to act on. Already confirmed
                to be one the caller participates in.

        Returns:
            A 409 ``Response`` carrying :data:`MULTIPLAYER_REFUSAL`, or None
            when the session is a solo one this API can play.
        """
        if session.status == GameSessionStatus.LOBBY:
            return Response(dict(MULTIPLAYER_REFUSAL), status=409)
        if GameSessionParticipant.objects.filter(session=session).count() > 1:
            return Response(dict(MULTIPLAYER_REFUSAL), status=409)
        return None


class SpotGuessrSessionScopedView(SoloSessionOnlyMixin, ExternalApiView):
    """Base for every endpoint addressed by a session id.

    Holds the two rules those endpoints share: the caller must participate in
    the session, and the session must be solo. Resolving both in one place is
    what keeps a new endpoint from accidentally being the one that answers 403,
    or the one that forgets the solo check.
    """

    def get_participant_session(self, request: Request, session_id: int) -> GameSession | None:
        """The session, only if the calling credential's owner participates in it.

        Any participant status qualifies (INVITED as well as JOINED), matching
        the internal controller: an invitee may look at a session before
        deciding about it.

        Args:
            request: The authenticated request.
            session_id: The session's primary key, straight from the URL.

        Returns:
            The session, or None when it does not exist *or* is somebody
            else's - never distinguished, since a sequential id would otherwise
            let a caller count the site's games.
        """
        participant = GameSessionParticipant.objects.filter(session_id=session_id, profile__user=request.user).select_related("session").first()
        return participant.session if participant is not None else None

    def resolve_solo_session(self, request: Request, session_id: int) -> tuple[GameSession | None, Response | None]:
        """Resolve and vet a session in one call.

        Args:
            request: The authenticated request.
            session_id: The session's primary key.

        Returns:
            ``(session, None)`` when the caller may proceed, or
            ``(None, response)`` carrying the 404 or 409 to return as-is.
        """
        session = self.get_participant_session(request, session_id)
        if session is None:
            return None, Response({"error": "Not found."}, status=404)
        refusal = self.refuse_if_multiplayer(session)
        if refusal is not None:
            return None, refusal
        return session, None


class SpotGuessrOverviewView(ExternalApiView):
    """GET: the SpotGuessr start screen - settings, limits, your rating, and what to resume.

    The web route of the same name renders HTML, so this is a genuinely new
    endpoint rather than a re-exposure of an existing JSON one. It deliberately
    omits the web page's mode-card icons/descriptions and its reversed URL
    templates: those describe how one client draws the screen, and baking them
    into a public contract would make a CSS decision a breaking change.

    ``friend_ratings`` is a separate, separately-gated block. A credential
    holding only ``games:read`` is asking about *its own* play; naming the
    user's friends is a social read, so the key is present only when the
    credential also holds ``social:read``, and each friend's identity is
    resolved through ``resolve_visible_identity`` so a friend who restricts
    their profile stays masked here too.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.GAMES_READ}),
    }

    @extend_schema(responses={200: SpotGuessrOverviewSerializer})
    def get(self, request: Request) -> Response:
        """Return the overview payload for the calling credential's owner."""
        profile = request.user.profile
        preference = spotguessr_overview.get_preference(profile)

        payload: dict[str, Any] = {
            "modes": [{"value": value, "label": label} for value, label in SpotGuessrMode.choices],
            "min_rounds": spotguessr_session.MIN_ROUNDS_PER_SESSION,
            "max_rounds": spotguessr_session.MAX_ROUNDS_PER_SESSION,
            "default_rounds": spotguessr_session.DEFAULT_ROUNDS_PER_SESSION,
            "round_time_limit_choices": list(spotguessr_session.ROUND_TIME_LIMIT_CHOICES),
            "last_config": preference.last_config or {},
            "show_ratings_to_friends": preference.show_ratings_to_friends,
            "own_rating": _rating_payload(spotguessr_overview.most_recent_rating(profile)),
            "active_session_id": spotguessr_overview.active_solo_session_id(profile),
        }

        if credential_grants(request.auth, {ApiKeyScope.SOCIAL_READ}):
            payload["friend_ratings"] = self._friend_ratings(profile)
        return Response(payload)

    def _friend_ratings(self, profile: Profile) -> list[dict[str, Any]]:
        """Each visible friend's rating, with their identity resolved for *profile*.

        Args:
            profile: The viewer.

        Returns:
            One entry per friend who has not opted out of showing ratings. A
            masked friend carries no slug at all: the slug is a stable handle
            that would identify them across requests and defeat the masking.
        """
        rows = []
        for entry in visible_friend_ratings(profile):
            friend: Profile = entry["profile"]
            identity = resolve_visible_identity(profile, friend)
            rows.append(
                {
                    "profile_slug": None if identity["is_masked"] else friend.slug,
                    "display_name": identity["display_name"],
                    "is_masked": identity["is_masked"],
                    "rating": _rating_payload(entry["rating"]),
                }
            )
        return rows


class SpotGuessrPreferencesView(ExternalApiView):
    """PATCH: update the one genuinely user-editable SpotGuessr preference.

    Mirrors ``controllers.spotguessr.SpotGuessrSettingsView`` exactly.
    ``last_config`` stays off this surface entirely - it is auto-managed by
    ``remember_last_config`` on every session start, never directly
    user-writable (confirmed against the internal view, which also only ever
    touches ``show_ratings_to_friends``).
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "PATCH": frozenset({ApiKeyScope.GAMES_WRITE}),
    }

    @extend_schema(request=SpotGuessrPreferencesUpdateSerializer, responses={200: SpotGuessrPreferencesResponseSerializer})
    def patch(self, request: Request) -> Response:
        """Write the caller's ``show_ratings_to_friends`` preference."""
        serializer = SpotGuessrPreferencesUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        preference = spotguessr_overview.get_preference(request.user.profile)
        preference.show_ratings_to_friends = serializer.validated_data["show_ratings_to_friends"]
        preference.save(update_fields=["show_ratings_to_friends", "updated"])
        return Response({"show_ratings_to_friends": preference.show_ratings_to_friends})


class SpotGuessrEligibleCountView(ExternalApiView):
    """GET: how many of the caller's own pins fall inside a candidate area.

    Mirrors ``controllers.spotguessr.SpotGuessrAreaPinCountView`` exactly - a
    lightweight pre-check so a client can warn "not enough pins here" before
    spending the (tighter, billed-imagery-capable) session-start budget on a
    config that has nothing to play.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.GAMES_READ}),
    }

    @extend_schema(parameters=[SpotGuessrEligibleCountQuerySerializer], responses={200: SpotGuessrEligibleCountResponseSerializer, 400: ErrorSerializer})
    def get(self, request: Request) -> Response:
        """Return the count of the caller's own pins inside ``geo_bounds``."""
        query = SpotGuessrEligibleCountQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)

        geo_bounds_geojson, error = _parse_geo_bounds_query(query.validated_data["geo_bounds"])
        if error is not None:
            return error

        geo_bounds = spotguessr_session.GameConfig(geo_bounds_geojson=geo_bounds_geojson).geo_bounds
        count = Pin.objects.filter(profile=request.user.profile, location__point__within=geo_bounds).count()
        return Response({"count": count})


class SpotGuessrEligiblePinsView(PaginatedListMixin, ExternalApiView):
    """GET: the caller's own pins that are currently SpotGuessr-eligible.

    A browse/read endpoint, not a play mode - solo eligibility is exactly "the
    player's own pinned locations" (see ``services.spotguessr.eligibility``),
    optionally narrowed to a candidate ``geo_bounds`` the same way session
    start and the area-count pre-check are. Mirrors
    ``controllers.spotguessr.SpotGuessrPinsView``, paginated instead of
    returned as one unbounded list.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.GAMES_READ}),
    }

    @extend_schema(parameters=[SpotGuessrEligiblePinsQuerySerializer], responses={200: SpotGuessrEligiblePinSerializer(many=True), 400: ErrorSerializer})
    def get(self, request: Request) -> Response:
        """Return one page of the caller's own pins, as candidate locations."""
        query = SpotGuessrEligiblePinsQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)

        pins = Pin.objects.filter(profile=request.user.profile).select_related("location").order_by("pk")

        raw_bounds = query.validated_data.get("geo_bounds")
        if raw_bounds:
            geo_bounds_geojson, error = _parse_geo_bounds_query(raw_bounds)
            if error is not None:
                return error
            geo_bounds = spotguessr_session.GameConfig(geo_bounds_geojson=geo_bounds_geojson).geo_bounds
            pins = pins.filter(location__point__within=geo_bounds)

        return self.paginated_response(
            pins,
            SpotGuessrEligiblePinSerializer,
            request,
            row_builder=lambda pin: {
                "label": pin.get_unique_search_name() or pin.name or "Unnamed pin",
                "latitude": pin.effective_latitude,
                "longitude": pin.effective_longitude,
            },
        )


class SpotGuessrSessionsView(PaginatedListMixin, ExternalApiView):
    """GET: the caller's session history. POST: start a new solo session.

    The list has no internal equivalent at all - the web page resumes from a
    JavaScript variable it was rendered with, which a native client that was
    backgrounded and killed has no access to. It is what makes "come back to
    the app and carry on" possible, so it deliberately includes finished and
    multiplayer sessions too, each flagged ``is_multiplayer`` so a client knows
    up front which ones this API will refuse to play.

    POST starts a solo session *and* generates its first round, because a
    session without one is not something a client can do anything with. A
    config with nothing eligible to play answers 409 and creates nothing. (The
    internal endpoint answers 200 with an ``error_code`` for that case; a 200
    that means "your request did not happen" is a wart, and mirroring it would
    make every generated client treat the failure as success.)
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.GAMES_READ}),
        "POST": frozenset({ApiKeyScope.GAMES_WRITE}),
    }
    #: The start throttle counts only the write tier, so listing sessions is not
    #: charged against the (deliberately tight) start budget. See
    #: ``throttling.GameStartThrottle`` for why starting needs its own cap.
    throttle_classes: ClassVar[list] = [ExternalApiBurstThrottle, ExternalApiReadThrottle, ExternalApiWriteThrottle, GameStartThrottle]

    @extend_schema(parameters=[SpotGuessrSessionListQuerySerializer], responses={200: SpotGuessrSessionSerializer(many=True)})
    def get(self, request: Request) -> Response:
        """Return one page of the caller's sessions, newest first."""
        serializer = SpotGuessrSessionListQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        profile = request.user.profile

        sessions = spotguessr_overview.participated_sessions(profile, status=serializer.validated_data.get("status"))
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(sessions, request, view=self)
        rows = page or []
        host_slugs = _host_slugs(rows)
        payload = [build_session_payload(session, host_slug=host_slugs.get(session.host_profile_id)) for session in rows]
        return paginator.get_paginated_response(SpotGuessrSessionSerializer(payload, many=True).data)

    @extend_schema(
        request=SpotGuessrSessionCreateSerializer,
        responses={201: SpotGuessrSessionCreateResponseSerializer, 400: ErrorSerializer, 409: ErrorSerializer},
    )
    def post(self, request: Request) -> Response:
        """Start a solo session and return it together with its first round."""
        serializer = SpotGuessrSessionCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        profile = request.user.profile

        config = spotguessr_session.GameConfig(
            difficulty=data.get("difficulty", 0.5),
            allow_arbitrary_external_photos=data.get("allow_arbitrary_external_photos", False),
            require_visited_all=data.get("require_visited_all", False),
            date_guessing_enabled=data.get("date_guessing_enabled", False),
            use_aliases=data.get("use_aliases", True),
            geo_bounds_geojson=data.get("geo_bounds"),
            round_time_limit_seconds=data.get("round_time_limit_seconds"),
        )
        # Saved before the game starts, exactly as the web start does, so the
        # next start on *either* client opens with these settings.
        spotguessr_overview.remember_last_config(profile, config)

        total_rounds = data.get("total_rounds", spotguessr_session.DEFAULT_ROUNDS_PER_SESSION)
        try:
            result = spotguessr_session.start_solo_playthrough(profile, data["mode"], config, total_rounds=total_rounds)
        except spotguessr_session.SpotGuessrError as exc:
            return Response({"error": str(exc)}, status=400)

        if result.round is None or result.session is None:
            return Response(
                {
                    "error": "You have nothing to play yet - pin some places (inside the chosen area, if you set one) and try again.",
                    "error_code": "no_eligible_locations",
                },
                status=409,
            )

        session = result.session
        return Response(
            {
                "session_id": session.pk,
                "mode": session.mode,
                "status": session.status,
                "total_rounds": session.total_rounds,
                "finished": False,
                "round": build_round_payload(result.round),
            },
            status=201,
        )


def _host_slugs(sessions: list[GameSession]) -> dict[int, str | None]:
    """Map the host profile id of each session to that profile's slug.

    Resolved in one query for the whole page rather than per row, and existing
    at all because host *ids* must never reach the wire.

    Args:
        sessions: The page of sessions being serialized.

    Returns:
        ``{profile_id: slug}`` for every host in the page.
    """
    host_ids = {session.host_profile_id for session in sessions}
    return dict(Profile.objects.filter(pk__in=host_ids).values_list("pk", "slug"))


class SpotGuessrSessionDetailView(SpotGuessrSessionScopedView):
    """GET: one session's own row, for a client resuming a specific game."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.GAMES_READ}),
    }

    @extend_schema(responses={200: SpotGuessrSessionSerializer, 404: ErrorSerializer, 409: ErrorSerializer})
    def get(self, request: Request, session_id: int) -> Response:
        """Return one of the caller's own solo sessions."""
        session, refusal = self.resolve_solo_session(request, session_id)
        if session is None:
            return refusal

        annotated = spotguessr_overview.participated_sessions(request.user.profile).filter(pk=session.pk).first()
        if annotated is None:
            return Response({"error": "Not found."}, status=404)
        host_slugs = _host_slugs([annotated])
        return Response(SpotGuessrSessionSerializer(build_session_payload(annotated, host_slug=host_slugs.get(annotated.host_profile_id))).data)


class SpotGuessrRoundView(SpotGuessrSessionScopedView):
    """GET: the session's current round, creating the next one when the last is done.

    Read-shaped but emphatically not a read. This call generates rounds, can
    complete the session, and persists the session's frozen ``bonus_scope`` on
    its first invocation, so it is neither idempotent nor cacheable - hence the
    explicit write tier below. Left in the hourly read budget it would sit in a
    bucket sized for a mobile client's bulk sync, which is exactly the wrong
    ceiling for a call that runs eligibility passes and can bill a Street View
    fetch.
    """

    #: ``games:write`` as well as ``games:read``, because this GET *writes* -
    #: see the class docstring. Reclassifying it into the write throttle tier
    #: (below) bounded how often it could be called but authorized nothing: a
    #: read-only credential could still generate rounds, freeze the session's
    #: ``bonus_scope`` and complete the session. A throttle is not a permission.
    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.GAMES_READ, ApiKeyScope.GAMES_WRITE}),
    }
    #: Overrides the scope-derived classification: ``games:read`` would
    #: otherwise put this in the read tier. See the class docstring.
    throttle_tier_by_method: ClassVar[dict[str, str]] = {"GET": TIER_WRITE}

    @extend_schema(responses={200: SpotGuessrRoundResponseSerializer, 404: ErrorSerializer, 409: ErrorSerializer})
    def get(self, request: Request, session_id: int) -> Response:
        """Return the round to play now, the final summary, or the empty state."""
        session, refusal = self.resolve_solo_session(request, session_id)
        if session is None:
            return refusal

        try:
            round_ = spotguessr_session.get_or_create_round(session)
        except spotguessr_session.SpotGuessrError as exc:
            return Response({"error": str(exc)}, status=400)

        if round_ is None:
            if spotguessr_session.rounds_played(session) == 0:
                # Never played a round, so there is no result to report. Saying
                # "finished" here would render as a real game that scored zero.
                return Response({"finished": False, "no_eligible_locations": True})
            spotguessr_session.complete_session(session)
            return Response({"finished": True, "summary": _summary_payload(session)})

        return Response({"finished": False, "round": build_round_payload(round_)})


class SpotGuessrGuessView(SpotGuessrSessionScopedView):
    """POST: submit this session's guess for one round.

    Scoring, the date bonus, the country/state/city bonus, the Glicko-2 update,
    the reveal, and advancing to the next round all happen inside
    ``services.spotguessr.session.submit_guess`` - which is also where the row
    lock that makes a concurrent double-submit safe lives. This view therefore
    neither re-runs any rating math nor wraps the call in a transaction of its
    own: an outer ``atomic()`` would extend that lock across the broadcast and
    next-round generation, turning a correctly-scoped critical section into a
    session-wide one.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.GAMES_WRITE}),
    }

    @extend_schema(request=SpotGuessrGuessSerializer, responses={200: SpotGuessrGuessResponseSerializer, 400: ErrorSerializer, 404: ErrorSerializer, 409: ErrorSerializer})
    def post(self, request: Request, session_id: int, round_id: int) -> Response:
        """Score one guess and return its result, with the answer once revealed."""
        from django.contrib.gis.geos import Point

        session, refusal = self.resolve_solo_session(request, session_id)
        if session is None:
            return refusal

        round_ = GameRound.objects.filter(pk=round_id, session=session).select_related("session", "location", "image").first()
        if round_ is None:
            return Response({"error": "Not found."}, status=404)

        serializer = SpotGuessrGuessSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # Point takes (x, y) - longitude first. Getting this backwards does not
        # raise anywhere: it silently produces a valid point somewhere else on
        # Earth, so every guess scores as a miss and the game merely looks
        # broken rather than erroring. Asserted directly in the tests.
        guess_point = Point(data["longitude"], data["latitude"], srid=4326)

        try:
            guess, bonus_tiers, rating_change = spotguessr_session.submit_guess(round_, request.user.profile, guess_point, data.get("guessed_date"))
        except spotguessr_session.SpotGuessrError as exc:
            # Covers the duplicate guess (the unique constraint, caught under
            # the row lock) and "you never joined this session".
            return Response({"error": str(exc)}, status=400)

        round_.refresh_from_db()
        return Response(build_reveal_payload(round_, guess, list(bonus_tiers), rating_change))


class SpotGuessrRoundExpireView(SpotGuessrSessionScopedView):
    """POST: force-reveal the current round because its round timer expired.

    Mirrors ``controllers.spotguessr.SpotGuessrRoundTimeoutView``. The
    authoritative check is server-side (``round_.created`` plus the session's
    own ``round_time_limit_seconds``, never the client's clock) - this just
    gives a client whose local countdown hit zero a fast path to
    ``expire_round_timer``. A no-op (200, not an error) when the round is
    already revealed or the timer genuinely hasn't expired yet - a
    late/duplicate/clock-skewed call is harmless either way.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.GAMES_WRITE}),
    }

    @extend_schema(responses={200: SpotGuessrRoundExpireResponseSerializer, 404: ErrorSerializer, 409: ErrorSerializer})
    def post(self, request: Request, session_id: int, round_id: int) -> Response:
        """Reveal the round if its timer has genuinely expired, and report its state."""
        session, refusal = self.resolve_solo_session(request, session_id)
        if session is None:
            return refusal

        round_ = GameRound.objects.filter(pk=round_id, session=session).first()
        if round_ is None:
            return Response({"error": "Not found."}, status=404)

        time_limit = (session.config or {}).get("round_time_limit_seconds")
        if round_.revealed_at is None and time_limit and timezone.now() >= round_.created + timedelta(seconds=time_limit):
            spotguessr_session.expire_round_timer(round_)
            round_.refresh_from_db()

        return Response({"revealed": round_.revealed_at is not None})


class SpotGuessrRoundFeedbackView(SpotGuessrSessionScopedView):
    """POST: thumbs up/down, or report, the photo a Photos-mode round just showed.

    Mirrors ``controllers.spotguessr.SpotGuessrPhotoFeedbackView``. Feeds
    ``services.media_relevance.effective_relevance`` at a reduced weight (or,
    for a report, full weight against "not relevant") - see
    ``services.spotguessr.relevance`` for exactly how. A 400 for a round with
    no photo (Named Place/Street View); a 403 for a round this profile never
    guessed on - there is no "reaction to a photo you weren't shown" case to
    support.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.GAMES_WRITE}),
    }

    @extend_schema(request=SpotGuessrFeedbackSerializer, responses={200: SpotGuessrFeedbackResponseSerializer, 400: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer, 409: ErrorSerializer})
    def post(self, request: Request, session_id: int, round_id: int) -> Response:
        """Record the caller's reaction to the round's photo, if it has one."""
        session, refusal = self.resolve_solo_session(request, session_id)
        if session is None:
            return refusal

        round_ = GameRound.objects.filter(pk=round_id, session=session).select_related("session").first()
        if round_ is None:
            return Response({"error": "Not found."}, status=404)

        serializer = SpotGuessrFeedbackSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        kind = serializer.validated_data["kind"]

        if not Guess.objects.filter(round=round_, profile=request.user.profile).exists():
            return Response({"error": "You can only react to a round you've guessed on."}, status=403)

        feedback = spotguessr_relevance.record_feedback(round_, request.user.profile, kind)
        if feedback is None:
            return Response({"error": "This round has no photo to react to."}, status=400)
        return Response({"kind": feedback.kind})


class SpotGuessrSummaryView(SpotGuessrSessionScopedView):
    """GET: the session's final scoreboard, including its rating movement."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.GAMES_READ}),
    }

    @extend_schema(responses={200: SpotGuessrSummarySerializer, 404: ErrorSerializer, 409: ErrorSerializer})
    def get(self, request: Request, session_id: int) -> Response:
        """Return the scoreboard for one of the caller's own solo sessions."""
        session, refusal = self.resolve_solo_session(request, session_id)
        if session is None:
            return refusal
        return Response(_summary_payload(session))


def _summary_payload(session: GameSession) -> dict[str, Any]:
    """The service's session summary, with profile ids swapped for slugs.

    The summary itself (points, rating delta, best round) is computed by
    ``services.spotguessr.session.session_summary`` and not recomputed here -
    this only rewrites the identifier, because the internal payload is keyed by
    primary key and those must not leave the server.

    Args:
        session: The session to summarize.

    Returns:
        The summary with each participant carrying ``profile_slug`` instead of
        ``profile_id``.
    """
    summary = spotguessr_session.session_summary(session)
    rows = summary.get("participants", [])
    slugs = dict(Profile.objects.filter(pk__in=[row["profile_id"] for row in rows]).values_list("pk", "slug"))
    summary["participants"] = [{**{key: value for key, value in row.items() if key != "profile_id"}, "profile_slug": slugs.get(row["profile_id"])} for row in rows]
    return summary


class SpotGuessrRoundImageView(SoloSessionOnlyMixin, ExternalApiView):
    """GET: the round's photo as bytes, with every metadata block removed.

    A native client cannot render the ``image_url`` on the round payload
    without also holding ``media:read`` and following the media gate, and even
    then it would receive the *original* file. That file routinely still
    carries the camera's GPS tags, which point straight at the answer - so this
    endpoint exists specifically to hand over a copy that does not, and it
    requires both ``games:read`` (this is game content) and ``media:read``
    (these are the user's own photo bytes). ``HasApiKeyScope`` requires every
    declared scope, so a credential holding only one of the pair gets nothing.

    Charged against the media budget rather than the API read budget, for the
    same reason the media gate is: a screen of images is a burst of file
    requests, and metering them against the JSON caps would refuse legitimate
    play while still leaving a leaked key able to pull files all day.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.GAMES_READ, ApiKeyScope.MEDIA_READ}),
    }
    throttle_classes: ClassVar[list] = [ExternalApiBurstThrottle, ExternalApiMediaThrottle]

    @extend_schema(responses={200: bytes, 404: ErrorSerializer, 409: ErrorSerializer})
    def get(self, request: Request, session_id: int, round_id: int) -> HttpResponse | Response:
        """Return the EXIF-stripped bytes of one round's photo."""
        participant = GameSessionParticipant.objects.filter(session_id=session_id, profile__user=request.user).select_related("session").first()
        if participant is None:
            return Response({"error": "Not found."}, status=404)
        refusal = self.refuse_if_multiplayer(participant.session)
        if refusal is not None:
            return refusal

        round_ = GameRound.objects.filter(pk=round_id, session_id=session_id).select_related("image").first()
        if round_ is None or round_.image is None:
            # A round in a text-only mode is genuinely "no such image" here,
            # and is reported identically to a round that isn't the caller's.
            return Response({"error": "Not found."}, status=404)

        try:
            payload, content_type = spotguessr_round_image.stripped_round_image(round_.image)
        except spotguessr_round_image.RoundImageUnavailableError:
            return Response({"error": "Not found."}, status=404)

        response = HttpResponse(payload, content_type=content_type)
        # Private: these bytes are one user's photo served under their own
        # credential, and a shared cache holding them would serve them to the
        # next caller of the same URL.
        response["Cache-Control"] = "private, max-age=300"
        return response
