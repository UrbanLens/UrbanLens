"""Request validation and wire shapes for the external API's games domain.

Two rules shape everything here, and both exist because of specific defects
this surface is built to avoid.

**Round payloads are whitelists, never passthroughs.** The internal
``services.spotguessr.serializers.serialize_round`` dict is built for the web
client and grows whenever a mode gains a field. Forwarding it wholesale is how
``image_caption`` - EXIF-derived text that routinely names the place - ended up
in a payload a player receives *before* guessing. :func:`build_round_payload`
therefore copies an explicit list of keys, so the next field somebody adds to a
mode strategy cannot silently become part of the question.

**Identities go out as slugs, never primary keys.** ``GameSession`` extends
``DashboardModel``, which has no uuid, so session ids on this surface are
sequential global integers (acceptable: a non-participant gets an
indistinguishable 404 either way). Profiles are a different matter - they do
have slugs, and leaking their sequential pks would hand a caller a way to count
and probe accounts it was never shown.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rest_framework import serializers

from urbanlens.dashboard.models.spotguessr.model import GameSessionStatus, SpotGuessrMode
from urbanlens.dashboard.services.spotguessr import relevance as spotguessr_relevance, session as spotguessr_session

if TYPE_CHECKING:
    from urbanlens.dashboard.models.spotguessr.model import GameRound, GameSession, Guess

#: Exactly the keys an external client may see for an unguessed round. Anything
#: ``serialize_round`` adds that is not listed here is dropped - see the module
#: docstring for the leak that motivates the whitelist rather than a blocklist.
#:
#: ``image_url`` is the media-gate URL for the round's photo; fetching it needs
#: ``media:read`` in addition to ``games:read``, exactly as any other photo does.
ROUND_PAYLOAD_FIELDS: tuple[str, ...] = (
    "round_id",
    "session_id",
    "mode",
    "sequence_index",
    "revealed",
    "geo_bounds",
    "shows_imagery",
    "expires_at",
    "image_url",
    "display_text",
    "street_view_image",
)

#: Exactly the keys an external client may see for its own guess's result. The
#: answer fields (``actual_latitude``/``actual_longitude``/``location_name``/
#: ``image_caption``) are only ever present in the source dict once the round is
#: genuinely revealed - listing them here does not make them appear early.
REVEAL_PAYLOAD_FIELDS: tuple[str, ...] = (
    "round_id",
    "distance_meters",
    "points",
    "date_points",
    "bonus_points",
    "bonus_tiers",
    "revealed",
    "rating_delta",
    "actual_latitude",
    "actual_longitude",
    "location_name",
    "image_caption",
)


def build_round_payload(round_: GameRound) -> dict[str, Any]:
    """Whitelist the internal round payload down to what an external client may see.

    Args:
        round_: The round to describe. Must not be relied upon to be
            unrevealed - the payload is answer-free either way, because the
            internal serializer never puts the answer in it.

    Returns:
        A dict holding only the keys in :data:`ROUND_PAYLOAD_FIELDS` that the
        internal serializer actually produced for this round's mode. Absent
        keys are omitted rather than nulled, so a Named Place round carries no
        ``image_url`` key at all.
    """
    from urbanlens.dashboard.services.spotguessr import serializers as spotguessr_serializers

    internal = spotguessr_serializers.serialize_round(round_)
    return {key: internal[key] for key in ROUND_PAYLOAD_FIELDS if key in internal}


def build_reveal_payload(round_: GameRound, guess: Guess, bonus_tiers: list[str], rating_change: Any) -> dict[str, Any]:
    """Whitelist the internal guess-result payload for an external client.

    Args:
        round_: The round just guessed, re-read so ``revealed_at`` is current.
        guess: The saved guess.
        bonus_tiers: Which country/state/city tiers the guess matched - not
            persisted on ``Guess``, so it only exists on the return of
            ``submit_guess``.
        rating_change: The guesser's own Glicko-2 change for this round, or
            None when the reveal is still withheld.

    Returns:
        A dict holding only the keys in :data:`REVEAL_PAYLOAD_FIELDS` that the
        internal serializer produced.
    """
    from urbanlens.dashboard.services.spotguessr import serializers as spotguessr_serializers

    internal = spotguessr_serializers.serialize_reveal(round_, guess, bonus_tiers, rating_change)
    return {key: internal[key] for key in REVEAL_PAYLOAD_FIELDS if key in internal}


class SpotGuessrModeSerializer(serializers.Serializer):
    """One selectable game mode."""

    value = serializers.CharField(read_only=True)
    label = serializers.CharField(read_only=True)


class PlayerRatingSerializer(serializers.Serializer):
    """A player's Glicko-2 standing in one mode, on the display (Elo-familiar) scale."""

    mode = serializers.CharField(read_only=True)
    rating = serializers.FloatField(read_only=True)
    rating_deviation = serializers.FloatField(read_only=True)
    games_played = serializers.IntegerField(read_only=True)
    last_played_at = serializers.DateTimeField(read_only=True, allow_null=True)


class FriendRatingSerializer(serializers.Serializer):
    """One friend's visible rating, with their identity already resolved for the viewer.

    ``display_name`` and ``profile_slug`` come from
    ``services.identity_visibility.resolve_visible_identity``, so a friend who
    restricts profile visibility is masked here exactly as they are everywhere
    else rather than being named because they happen to play a game.
    """

    profile_slug = serializers.CharField(read_only=True, allow_null=True)
    display_name = serializers.CharField(read_only=True)
    is_masked = serializers.BooleanField(read_only=True)
    rating = PlayerRatingSerializer(read_only=True, allow_null=True)


class SpotGuessrOverviewSerializer(serializers.Serializer):
    """Everything a client needs to draw the SpotGuessr start screen."""

    modes = SpotGuessrModeSerializer(many=True, read_only=True)
    min_rounds = serializers.IntegerField(read_only=True)
    max_rounds = serializers.IntegerField(read_only=True)
    default_rounds = serializers.IntegerField(read_only=True)
    round_time_limit_choices = serializers.ListField(child=serializers.IntegerField(), read_only=True)
    last_config = serializers.DictField(read_only=True)
    show_ratings_to_friends = serializers.BooleanField(read_only=True)
    own_rating = PlayerRatingSerializer(read_only=True, allow_null=True)
    active_session_id = serializers.IntegerField(read_only=True, allow_null=True)
    friend_ratings = FriendRatingSerializer(many=True, read_only=True, required=False)


class SpotGuessrSessionCreateSerializer(serializers.Serializer):
    """The body of a solo session start.

    Deliberately has no ``invite_profile_ids``: multiplayer is web-only on this
    surface (see ``views_games.SoloSessionOnlyMixin``), and accepting the field
    would let a client create a lobby it can then never play.
    """

    mode = serializers.ChoiceField(choices=SpotGuessrMode.choices, default=SpotGuessrMode.PHOTOS)
    #: Not bounded by the field itself: the service clamps into
    #: ``[MIN_ROUNDS_PER_SESSION, MAX_ROUNDS_PER_SESSION]`` and clamping is
    #: friendlier than a 400 for a client that guessed the limits wrong.
    total_rounds = serializers.IntegerField(required=False, min_value=1, max_value=1000)
    difficulty = serializers.FloatField(required=False, min_value=0.0, max_value=1.0)
    allow_arbitrary_external_photos = serializers.BooleanField(required=False, default=False)
    require_visited_all = serializers.BooleanField(required=False, default=False)
    date_guessing_enabled = serializers.BooleanField(required=False, default=False)
    use_aliases = serializers.BooleanField(required=False, default=True)
    round_time_limit_seconds = serializers.ChoiceField(
        choices=[(value, str(value)) for value in spotguessr_session.ROUND_TIME_LIMIT_CHOICES],
        required=False,
        allow_null=True,
    )
    #: A GeoJSON *object*, not the JSON-encoded string the HTML form posts. A
    #: JSON API client already has a parsed object; making it re-encode one only
    #: to have the server parse it again is a needless round trip and an easy
    #: source of double-encoding bugs.
    geo_bounds = serializers.JSONField(required=False, allow_null=True)

    def validate_geo_bounds(self, value: Any) -> dict | None:
        """Reject anything that isn't a GEOS-parseable GeoJSON geometry.

        Parsing is forced here rather than left lazy so a malformed area comes
        back as a 400 naming the field, instead of a 500 from deep inside round
        generation minutes later.

        Args:
            value: The submitted ``geo_bounds`` value.

        Returns:
            The value unchanged when it parses, or None when it was null.

        Raises:
            rest_framework.serializers.ValidationError: When the value is not a
                JSON object or does not parse as a geometry.
        """
        from django.contrib.gis.gdal.error import GDALException
        from django.contrib.gis.geos import GEOSException

        if value is None:
            return None
        if not isinstance(value, dict):
            raise serializers.ValidationError("geo_bounds must be a GeoJSON object.")
        try:
            _ = spotguessr_session.GameConfig(geo_bounds_geojson=value).geo_bounds
        except (GEOSException, GDALException, ValueError, TypeError) as exc:
            raise serializers.ValidationError("geo_bounds must be a valid GeoJSON geometry.") from exc
        return value


class SpotGuessrRoundSerializer(serializers.Serializer):
    """One round as an external client sees it, before the answer is known.

    Documentation of :data:`ROUND_PAYLOAD_FIELDS` for the generated schema; the
    payload itself is built by :func:`build_round_payload`, which is the code
    that actually enforces the whitelist.
    """

    round_id = serializers.IntegerField(read_only=True)
    session_id = serializers.IntegerField(read_only=True)
    mode = serializers.CharField(read_only=True)
    sequence_index = serializers.IntegerField(read_only=True)
    revealed = serializers.BooleanField(read_only=True)
    geo_bounds = serializers.ListField(read_only=True, allow_null=True, required=False)
    shows_imagery = serializers.BooleanField(read_only=True)
    expires_at = serializers.CharField(read_only=True, allow_null=True, required=False)
    image_url = serializers.CharField(read_only=True, required=False)
    display_text = serializers.CharField(read_only=True, required=False, allow_null=True)
    street_view_image = serializers.CharField(read_only=True, required=False, allow_null=True)


class SpotGuessrSessionCreateResponseSerializer(serializers.Serializer):
    """The freshly started session, with its first round already generated."""

    session_id = serializers.IntegerField(read_only=True)
    mode = serializers.CharField(read_only=True)
    status = serializers.CharField(read_only=True)
    total_rounds = serializers.IntegerField(read_only=True)
    finished = serializers.BooleanField(read_only=True)
    round = SpotGuessrRoundSerializer(read_only=True)


class SpotGuessrParticipantSerializer(serializers.Serializer):
    """One participant's line on the final scoreboard."""

    profile_slug = serializers.CharField(read_only=True, allow_null=True)
    username = serializers.CharField(read_only=True)
    avatar_url = serializers.CharField(read_only=True, allow_null=True)
    total_points = serializers.IntegerField(read_only=True)
    rating_delta = serializers.FloatField(read_only=True)
    best_round_points = serializers.IntegerField(read_only=True, allow_null=True)
    best_round_distance_meters = serializers.FloatField(read_only=True, allow_null=True)


class SpotGuessrSummarySerializer(serializers.Serializer):
    """A session's final scoreboard."""

    session_id = serializers.IntegerField(read_only=True)
    mode = serializers.CharField(read_only=True)
    status = serializers.CharField(read_only=True)
    total_rounds = serializers.IntegerField(read_only=True)
    rounds_played = serializers.IntegerField(read_only=True)
    participants = SpotGuessrParticipantSerializer(many=True, read_only=True)


class SpotGuessrRoundResponseSerializer(serializers.Serializer):
    """The current-round response, which is one of three mutually exclusive states.

    ``no_eligible_locations`` is its own state rather than a finished game with
    zero rounds: a client that renders "Game over, 0 points" for a player who
    simply has nothing pinned in the chosen area is showing them a result they
    never earned.
    """

    finished = serializers.BooleanField(read_only=True)
    round = SpotGuessrRoundSerializer(read_only=True, required=False)
    summary = SpotGuessrSummarySerializer(read_only=True, required=False)
    no_eligible_locations = serializers.BooleanField(read_only=True, required=False)


class SpotGuessrGuessSerializer(serializers.Serializer):
    """A guess: where (and optionally when) the player thinks the round's location is."""

    latitude = serializers.FloatField(min_value=-90.0, max_value=90.0)
    longitude = serializers.FloatField(min_value=-180.0, max_value=180.0)
    #: Only scored when the session was configured with ``date_guessing_enabled``
    #: and the round's photo has a known capture date; otherwise ignored.
    guessed_date = serializers.DateField(required=False, allow_null=True)


class SpotGuessrGuessResponseSerializer(serializers.Serializer):
    """One guess's own score, plus the answer once the round is actually revealed."""

    round_id = serializers.IntegerField(read_only=True)
    distance_meters = serializers.FloatField(read_only=True, allow_null=True)
    points = serializers.IntegerField(read_only=True)
    date_points = serializers.IntegerField(read_only=True)
    bonus_points = serializers.IntegerField(read_only=True)
    bonus_tiers = serializers.ListField(child=serializers.CharField(), read_only=True)
    revealed = serializers.BooleanField(read_only=True)
    rating_delta = serializers.FloatField(read_only=True, allow_null=True)
    actual_latitude = serializers.FloatField(read_only=True, required=False)
    actual_longitude = serializers.FloatField(read_only=True, required=False)
    location_name = serializers.CharField(read_only=True, required=False, allow_null=True)
    image_caption = serializers.CharField(read_only=True, required=False, allow_null=True)


class SpotGuessrSessionSerializer(serializers.Serializer):
    """One row of the session list, and the whole body of the session detail.

    ``is_multiplayer`` is derived from the roster size rather than from the
    status, because a multiplayer game that has already begun is ACTIVE exactly
    like a solo one. Clients use it to know which sessions this API will refuse
    to play (see ``views_games.SoloSessionOnlyMixin``).
    """

    session_id = serializers.IntegerField(read_only=True)
    mode = serializers.CharField(read_only=True)
    status = serializers.CharField(read_only=True)
    total_rounds = serializers.IntegerField(read_only=True)
    rounds_played = serializers.IntegerField(read_only=True)
    started_at = serializers.DateTimeField(read_only=True)
    ended_at = serializers.DateTimeField(read_only=True, allow_null=True)
    is_multiplayer = serializers.BooleanField(read_only=True)
    host_profile_slug = serializers.CharField(read_only=True, allow_null=True)


class SpotGuessrSessionListQuerySerializer(serializers.Serializer):
    """Query parameters for the session list."""

    status = serializers.ChoiceField(choices=GameSessionStatus.choices, required=False)


class SpotGuessrRoundExpireResponseSerializer(serializers.Serializer):
    """Whether the round timer's expiry call actually revealed the round."""

    revealed = serializers.BooleanField(read_only=True)


class SpotGuessrFeedbackSerializer(serializers.Serializer):
    """A player's explicit reaction to the photo/imagery a round just showed."""

    kind = serializers.ChoiceField(choices=[(kind, kind) for kind in spotguessr_relevance.EXPLICIT_KINDS])


class SpotGuessrFeedbackResponseSerializer(serializers.Serializer):
    """The feedback kind actually recorded."""

    kind = serializers.CharField(read_only=True)


class SpotGuessrPreferencesUpdateSerializer(serializers.Serializer):
    """The one genuinely user-editable SpotGuessr preference.

    ``last_config`` is deliberately absent - it is auto-managed by
    ``remember_last_config`` on every session start, never directly
    user-writable (see ``services.spotguessr.overview``).
    """

    show_ratings_to_friends = serializers.BooleanField()


class SpotGuessrPreferencesResponseSerializer(serializers.Serializer):
    """The preference row after a write."""

    show_ratings_to_friends = serializers.BooleanField(read_only=True)


class SpotGuessrEligibleCountQuerySerializer(serializers.Serializer):
    """Query parameters for the eligible-pin-count pre-check."""

    #: A GeoJSON-encoded string, matching the internal
    #: ``SpotGuessrAreaPinCountView`` query param exactly - unlike the session
    #: create body, this travels in a query string, which has no native JSON
    #: type.
    geo_bounds = serializers.CharField()


class SpotGuessrEligibleCountResponseSerializer(serializers.Serializer):
    """How many of the caller's own pins fall inside a candidate area."""

    count = serializers.IntegerField(read_only=True)


class SpotGuessrEligiblePinsQuerySerializer(serializers.Serializer):
    """Query parameters for the eligible-pins browse feed."""

    #: Optional - omitting it lists every one of the caller's pins, matching
    #: solo play's own eligibility rule (see ``services.spotguessr.eligibility``).
    geo_bounds = serializers.CharField(required=False)


class SpotGuessrEligiblePinSerializer(serializers.Serializer):
    """One of the caller's own pins, as a candidate SpotGuessr location."""

    label = serializers.CharField(read_only=True)
    latitude = serializers.FloatField(read_only=True)
    longitude = serializers.FloatField(read_only=True)


def build_session_payload(session: GameSession, *, host_slug: str | None) -> dict[str, Any]:
    """Describe one session for the list and detail endpoints.

    Reads the ``rounds_played``/``participant_count`` annotations
    ``services.spotguessr.overview.participated_sessions`` attaches, so a page
    of rows costs one query rather than two per row.

    Args:
        session: An annotated session row.
        host_slug: The host's profile slug, resolved by the caller in bulk. The
            host's primary key is deliberately never sent.

    Returns:
        The session's wire payload.
    """
    return {
        "session_id": session.pk,
        "mode": session.mode,
        "status": session.status,
        "total_rounds": session.total_rounds,
        "rounds_played": getattr(session, "rounds_played", 0),
        "started_at": session.started_at,
        "ended_at": session.ended_at,
        "is_multiplayer": getattr(session, "participant_count", 1) > 1,
        "host_profile_slug": host_slug,
    }
