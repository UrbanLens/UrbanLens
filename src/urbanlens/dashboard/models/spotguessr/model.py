"""SpotGuessr models - Glicko-2 ratings, game sessions, rounds, and guesses.

See ``docs/designs/spotguessr.md`` for the full rules this schema encodes -
eligibility ("pinned by every participant"), point-vs-boundary distance
scoring, the difficulty slider, and the Glicko-2 player/location rating
pairing. Only ``SpotGuessrMode.PHOTOS`` has round-generation logic as of
UL-391; ``NAMED_PLACE``/``STREET_VIEW`` are defined now so later phases
(UL-393) don't need a schema migration just to add a mode.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib.gis.db.models import PointField
from django.db.models import (
    CASCADE,
    SET_NULL,
    BooleanField,
    CharField,
    DateField,
    DateTimeField,
    FloatField,
    ForeignKey,
    JSONField,
    OneToOneField,
    PositiveIntegerField,
    PositiveSmallIntegerField,
)
from django.db.models.constraints import UniqueConstraint

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.spotguessr.queryset import (
    GameRoundManager,
    GameSessionManager,
    GameSessionParticipantManager,
    GuessManager,
    LocationModeRatingManager,
    PlayerModeRatingManager,
)

#: Glicko-2's internal scale <-> the traditional (Elo-familiar) display scale,
#: per Glickman's "Example of the Glicko-2 system" (2012).
GLICKO2_SCALE = 173.7178
DEFAULT_RATING = 1500.0
DEFAULT_RATING_DEVIATION = 350.0
DEFAULT_VOLATILITY = 0.06

_DEFAULT_MU = 0.0
_DEFAULT_PHI = DEFAULT_RATING_DEVIATION / GLICKO2_SCALE


class SpotGuessrMode(abstract.TextChoices):
    """Which SpotGuessr game mode a rating/session/round belongs to."""

    PHOTOS = "photos", "Photos"
    NAMED_PLACE = "named_place", "Named Place"
    STREET_VIEW = "street_view", "Street View"


class GameSessionStatus(abstract.TextChoices):
    """Lifecycle of a GameSession."""

    ACTIVE = "active", "Active"
    COMPLETED = "completed", "Completed"
    ABANDONED = "abandoned", "Abandoned"


class _Glicko2RatingFields:
    """Shared display-scale conversion for PlayerModeRating and LocationModeRating.

    ``mu``/``phi``/``sigma`` are stored on the Glicko-2 paper's own internal
    scale (mu centered on 0, phi around 1-2) since that's what
    ``services.spotguessr.glicko2`` operates on directly. Everything
    user-facing reads ``rating``/``rating_deviation`` instead, so no caller
    outside the rating engine needs to know the scale constant.
    """

    if TYPE_CHECKING:
        mu: float
        phi: float

    @property
    def rating(self) -> float:
        """Display-scale rating (Elo/Glicko-familiar, centered on 1500)."""
        return DEFAULT_RATING + GLICKO2_SCALE * self.mu

    @property
    def rating_deviation(self) -> float:
        """Display-scale rating deviation (uncertainty; lower = more confident)."""
        return GLICKO2_SCALE * self.phi


class PlayerModeRating(_Glicko2RatingFields, abstract.DashboardModel):
    """A profile's Glicko-2 skill rating for one SpotGuessr mode.

    One row per ``(profile, mode)`` - a Photos-mode rating is tracked
    independently of a Street View-mode rating, since they're different
    skills. Updated once per round played (see
    ``services.spotguessr.ratings.apply_round_ratings``), treating the
    round's location as the round's sole "opponent."
    """

    mode = CharField(max_length=20, choices=SpotGuessrMode.choices)
    mu = FloatField(default=_DEFAULT_MU)
    phi = FloatField(default=_DEFAULT_PHI)
    sigma = FloatField(default=DEFAULT_VOLATILITY)
    games_played = PositiveIntegerField(default=0)
    last_played_at = DateTimeField(null=True, blank=True)

    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="spotguessr_ratings",
    )

    if TYPE_CHECKING:
        profile_id: int

    objects = PlayerModeRatingManager()

    def __str__(self) -> str:
        return f"PlayerModeRating(profile={self.profile_id}, mode={self.mode}, rating={self.rating:.0f})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_spotguessr_player_ratings"
        constraints = [
            UniqueConstraint(fields=["profile", "mode"], name="db_sg_player_rating_unique"),
        ]


class LocationModeRating(_Glicko2RatingFields, abstract.DashboardModel):
    """A location's Glicko-2 *difficulty* rating for one SpotGuessr mode.

    One row per ``(location, mode)`` - the same location can be easy as a
    Photos round and hard as a Street View round. Updated once per round
    played, treating every participant in that round as an "opponent" with
    outcome score ``1 - (that participant's normalized points)`` - a
    location nobody can find is "winning" against the field, which is
    exactly the high-difficulty signal a hard location should earn.
    """

    mode = CharField(max_length=20, choices=SpotGuessrMode.choices)
    mu = FloatField(default=_DEFAULT_MU)
    phi = FloatField(default=_DEFAULT_PHI)
    sigma = FloatField(default=DEFAULT_VOLATILITY)
    games_played = PositiveIntegerField(default=0)
    last_used_at = DateTimeField(null=True, blank=True)

    location = ForeignKey(
        "dashboard.Location",
        on_delete=CASCADE,
        related_name="spotguessr_ratings",
    )

    if TYPE_CHECKING:
        location_id: int

    objects = LocationModeRatingManager()

    def __str__(self) -> str:
        return f"LocationModeRating(location={self.location_id}, mode={self.mode}, rating={self.rating:.0f})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_spotguessr_location_ratings"
        constraints = [
            UniqueConstraint(fields=["location", "mode"], name="db_sg_location_rating_unique"),
        ]


class GameSession(abstract.DashboardModel):
    """One SpotGuessr playthrough: a mode, a config snapshot, a fixed round count.

    Modeled as a proper many-participant session from the start (see
    ``GameSessionParticipant``) even though UL-391 only ever creates
    single-participant sessions - every eligibility/scoring rule already
    reads "all participants," not "the player," so multiplayer (UL-392) can
    reuse these tables unchanged.

    Attributes:
        mode: Which game mode this session plays.
        status: Lifecycle state.
        config: Snapshot of the settings this session was started with -
            ``difficulty`` (0.0-1.0 slider), ``external_media_only``,
            ``require_visited_all``, ``date_guessing_enabled``, and
            ``geo_bounds`` (a GeoJSON polygon/bbox, or None). Snapshotted
            (not read live from preferences) so a session's rules stay
            consistent even if the host changes their defaults mid-game.
        total_rounds: Number of rounds this session will play.
        host_profile: Who started the session.
    """

    mode = CharField(max_length=20, choices=SpotGuessrMode.choices)
    status = CharField(max_length=12, choices=GameSessionStatus.choices, default=GameSessionStatus.ACTIVE)
    config = JSONField(default=dict)
    total_rounds = PositiveSmallIntegerField(default=5)
    started_at = DateTimeField(auto_now_add=True)
    ended_at = DateTimeField(null=True, blank=True)

    host_profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="hosted_spotguessr_sessions",
    )

    if TYPE_CHECKING:
        host_profile_id: int

    objects = GameSessionManager()

    @property
    def is_active(self) -> bool:
        """Whether this session is still in progress."""
        return self.status == GameSessionStatus.ACTIVE

    def __str__(self) -> str:
        return f"GameSession({self.mode}, host={self.host_profile_id}, status={self.status})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_spotguessr_sessions"


class GameSessionParticipant(abstract.DashboardModel):
    """One profile's membership in a GameSession, plus their running score.

    ``total_points`` is a denormalized cache (mirrors ``Pin.last_visited``'s
    role) kept in sync by ``services.spotguessr.session`` as guesses are
    submitted, so the scoreboard never needs to re-sum every guess.
    """

    total_points = PositiveIntegerField(default=0)
    joined_at = DateTimeField(auto_now_add=True)

    session = ForeignKey(
        "dashboard.GameSession",
        on_delete=CASCADE,
        related_name="participants",
    )
    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="spotguessr_participations",
    )

    if TYPE_CHECKING:
        session_id: int
        profile_id: int

    objects = GameSessionParticipantManager()

    def __str__(self) -> str:
        return f"GameSessionParticipant(session={self.session_id}, profile={self.profile_id})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_spotguessr_session_participants"
        constraints = [
            UniqueConstraint(fields=["session", "profile"], name="db_sg_participant_unique"),
        ]


class GameRound(abstract.DashboardModel):
    """One location to guess within a session.

    Attributes:
        session: The session this round belongs to.
        sequence_index: 0-based position within the session's round order.
        location: The answer.
        image: The photo shown (Photos mode only; null for other modes).
        target_is_point: Whether scoring measures from ``target_point``
            (the image had its own coordinates) rather than the location's
            *current* effective boundary. See ``docs/designs/spotguessr.md``
            ("Scoring: point vs. boundary distance") for why boundary-based
            rounds deliberately do NOT snapshot geometry - boundaries are
            community-maintained and get more accurate over time.
        target_point: Snapshot of the exact point used when
            ``target_is_point`` is True. A snapshot (not a live read of
            ``image.latitude``/``longitude``) because a photo's coordinates
            could later be corrected, and the round should stay consistent
            with what the player actually saw.
        revealed_at: When the answer became visible to at least one
            participant (immediately after guessing, in solo play).
    """

    sequence_index = PositiveSmallIntegerField()
    target_is_point = BooleanField(default=False)
    target_point = PointField(geography=True, srid=4326, null=True, blank=True)
    revealed_at = DateTimeField(null=True, blank=True)

    session = ForeignKey(
        "dashboard.GameSession",
        on_delete=CASCADE,
        related_name="rounds",
    )
    location = ForeignKey(
        "dashboard.Location",
        on_delete=CASCADE,
        related_name="spotguessr_rounds",
    )
    image = ForeignKey(
        "dashboard.Image",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="spotguessr_rounds",
    )

    if TYPE_CHECKING:
        session_id: int
        location_id: int
        image_id: int | None

    objects = GameRoundManager()

    def __str__(self) -> str:
        return f"GameRound(session={self.session_id}, sequence_index={self.sequence_index}, location={self.location_id})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_spotguessr_rounds"
        constraints = [
            UniqueConstraint(fields=["session", "sequence_index"], name="db_sg_round_unique"),
        ]


class Guess(abstract.DashboardModel):
    """One participant's answer to one GameRound.

    ``distance_meters``/``points``/``date_points`` are computed once at
    submission time (``services.spotguessr.scoring``) and stored, rather
    than recomputed on every read - a round's boundary-based target can
    drift as the community edits the boundary later, and a settled guess
    must not silently re-score itself when that happens.
    """

    guess_point = PointField(geography=True, srid=4326)
    distance_meters = FloatField(null=True, blank=True)
    points = PositiveIntegerField(default=0)
    guessed_date = DateField(null=True, blank=True)
    date_points = PositiveIntegerField(default=0)
    submitted_at = DateTimeField(auto_now_add=True)

    round = ForeignKey(
        "dashboard.GameRound",
        on_delete=CASCADE,
        related_name="guesses",
    )
    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="spotguessr_guesses",
    )

    if TYPE_CHECKING:
        round_id: int
        profile_id: int

    objects = GuessManager()

    def __str__(self) -> str:
        return f"Guess(round={self.round_id}, profile={self.profile_id}, points={self.points})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_spotguessr_guesses"
        constraints = [
            UniqueConstraint(fields=["round", "profile"], name="db_sg_guess_unique"),
        ]


class SpotGuessrPreference(abstract.DashboardModel):
    """Per-profile SpotGuessr settings - same shape as NotificationPreference/SafetyPreference.

    Attributes:
        show_ratings_to_friends: Whether this profile's per-mode ratings may
            appear on a friend's SpotGuessr overview page. Default True
            (opt-out), per spec.
        last_config: The player's last-used game settings (difficulty,
            toggles, geo bounds), mirroring ``Profile.home_widget_layout``'s
            "remember my preferences" role - returning to the game shouldn't
            reset the difficulty slider every time.
    """

    show_ratings_to_friends = BooleanField(default=True)
    last_config = JSONField(default=dict)

    profile = OneToOneField(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="spotguessr_preference",
    )

    if TYPE_CHECKING:
        profile_id: int

    def __str__(self) -> str:
        return f"SpotGuessrPreference(profile={self.profile_id})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_spotguessr_preferences"
