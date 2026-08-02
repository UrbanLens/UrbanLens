"""SpotGuessr models - Glicko-2 ratings, game sessions, rounds, guesses, and chat.

See ``docs/designs/spotguessr.md`` for the full rules this schema encodes -
eligibility ("pinned by every joined participant"), point-vs-boundary
distance scoring, the difficulty slider, the Glicko-2 player/location rating
pairing, and the multiplayer lobby lifecycle (UL-392).
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
    Index,
    JSONField,
    OneToOneField,
    PositiveIntegerField,
    PositiveSmallIntegerField,
)
from django.db.models.constraints import UniqueConstraint

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.spotguessr.queryset import (
    GameRoundManager,
    GameSessionChatMessageManager,
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
    """Lifecycle of a GameSession.

    Solo sessions skip LOBBY entirely (created directly as ACTIVE with one
    JOINED participant). Multiplayer sessions start in LOBBY and only
    become ACTIVE when the host explicitly begins the game - see
    ``docs/designs/spotguessr.md``'s "Multiplayer sessions" section.
    """

    LOBBY = "lobby", "Lobby"
    ACTIVE = "active", "Active"
    COMPLETED = "completed", "Completed"
    ABANDONED = "abandoned", "Abandoned"


class GameSessionParticipantStatus(abstract.TextChoices):
    """Whether a participant has accepted their invitation yet.

    Mirrors ``TripMembership.status`` - the same row that will hold the
    accepted membership *is* the invite record, so there is no separate
    invite model.
    """

    INVITED = "invited", "Invited"
    JOINED = "joined", "Joined"


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

    Modeled as a proper many-participant session from Phase 1 (see
    ``GameSessionParticipant``) - every eligibility/scoring rule reads "all
    (joined) participants," not "the player," so multiplayer (UL-392) reuses
    these tables unchanged; only ``GameSessionParticipant.status`` and the
    ``LOBBY`` status were added.

    Attributes:
        mode: Which game mode this session plays.
        status: Lifecycle state.
        config: Snapshot of the settings this session was started with -
            ``difficulty`` (0.0-1.0 slider), ``allow_arbitrary_external_photos``,
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

    Attributes:
        status: INVITED until the profile accepts, then JOINED. A solo
            session's host row is created directly as JOINED - there is no
            invite step when you're the only player. Eligibility, "has
            everyone in this round guessed," and the final scoreboard all
            read JOINED participants only (see
            ``docs/designs/spotguessr.md``'s eligibility rule 6) - an
            invitee who never accepts is not yet a player.
        joined_at: When this row was created. Despite the name, this is set
            for INVITED rows too (it's really "created_at" - kept as
            ``joined_at`` since every Phase 1 row really was a join, and a
            rename would be a needless migration for an internal field).
        rating_delta: Running total of this participant's net Glicko-2
            display-rating change across every round completed so far this
            session (see ``services.spotguessr.session._finish_round``) -
            surfaced on the summary screen so the game's rating movement
            isn't invisible (see the SpotGuessr audit's "the game computes
            your rating change every round and never shows it to you"
            finding). Meaningless outside the session it belongs to; not a
            player's overall rating (see ``PlayerModeRating`` for that).
    """

    status = CharField(max_length=10, choices=GameSessionParticipantStatus.choices, default=GameSessionParticipantStatus.JOINED)
    total_points = PositiveIntegerField(default=0)
    rating_delta = FloatField(default=0.0)
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

    @property
    def is_joined(self) -> bool:
        """Whether this participant has actually accepted (vs. still just invited)."""
        return self.status == GameSessionParticipantStatus.JOINED

    def __str__(self) -> str:
        return f"GameSessionParticipant(session={self.session_id}, profile={self.profile_id}, status={self.status})"

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
        display_text: The name/alias text shown (Named Place mode only;
            null for other modes). Snapshotted at round-creation time -
            when aliases are enabled, a random one is chosen once per round
            and must stay fixed for every participant and across
            reconnects, not re-rolled on every read. Street View mode shows
            no persisted text or photo - its imagery is re-fetched (cache-
            backed, see ``services.spotguessr.street_view``) from the
            location's coordinates each time.
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
    display_text = CharField(max_length=255, null=True, blank=True)
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

    ``distance_meters``/``points``/``date_points``/``bonus_points`` are
    computed once at submission time (``services.spotguessr.scoring``,
    ``services.spotguessr.geo_bonus``) and stored, rather than recomputed on
    every read - a round's boundary-based target can drift as the community
    edits the boundary later, and a settled guess must not silently re-score
    itself when that happens.
    """

    guess_point = PointField(geography=True, srid=4326)
    distance_meters = FloatField(null=True, blank=True)
    points = PositiveIntegerField(default=0)
    guessed_date = DateField(null=True, blank=True)
    date_points = PositiveIntegerField(default=0)
    #: Country/state/city bonus (services.spotguessr.geo_bonus) - unlike
    #: date_points, this folds into the Glicko outcome fraction (see
    #: services.spotguessr.ratings) since admin-area correctness is the same
    #: "know where this is" skill the rating measures.
    bonus_points = PositiveIntegerField(default=0)
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


class PhotoCoordinateGuess(abstract.DashboardModel):
    """One anonymized guess toward a photo's own coordinates.

    Recorded for every Photos-mode guess, whether or not the photo already
    has real coordinates - see ``services.spotguessr.photo_coordinates``'s
    ``record_guess`` docstring for why (currently only used to *estimate* a
    still-unplaced photo's position, but kept for every photo regardless in
    case it's useful later, e.g. for flagging/correcting a wrong placement).

    Deliberately carries no profile or round FK - see
    ``services.spotguessr.photo_coordinates`` for the full rationale. This is
    crowd-sourced signal toward the photo's position, not gameplay history;
    keeping it structurally impossible to trace back to who made a given
    guess is the point, not an incidental privacy nicety. ``created``
    (inherited) is the "datetime" this guess was made.
    """

    guess_point = PointField(geography=True, srid=4326)
    is_correct = BooleanField()

    image = ForeignKey(
        "dashboard.Image",
        on_delete=CASCADE,
        related_name="coordinate_guesses",
    )

    if TYPE_CHECKING:
        image_id: int

    def __str__(self) -> str:
        return f"PhotoCoordinateGuess(image={self.image_id}, is_correct={self.is_correct})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_spotguessr_photo_coordinate_guesses"
        indexes = [
            Index(fields=["image", "is_correct"], name="idxdb_sg_cordgues_iscorrect"),
        ]


class GamePhotoFeedbackKind(abstract.TextChoices):
    """What a participant did (or didn't do) about the photo shown in a Photos-mode round.

    See ``services.media.media_relevance.effective_relevance`` for how these feed
    into a photo's overall relevance score - notably, ``THUMBS_DOWN`` is
    recorded here but deliberately excluded from that score.
    """

    THUMBS_UP = "thumbs_up", "Thumbs Up"
    THUMBS_DOWN = "thumbs_down", "Thumbs Down"
    REPORTED = "reported", "Reported"
    NO_REACTION = "no_reaction", "No Reaction"


class GamePhotoFeedback(abstract.DashboardModel):
    """One participant's reaction to the photo shown in one Photos-mode round.

    An event log, not a per-profile mark like ``MediaRelevance`` - the same
    profile can (and, for ``NO_REACTION``, usually will) accumulate a fresh
    row every time they're shown the same photo again in a later round, since
    the whole point of the "shown, no reaction" signal is that it's very weak
    per-impression and only means something in aggregate over many plays. An
    explicit reaction (thumbs up/down, report) always overwrites whatever was
    recorded for that round instead - see ``services.spotguessr.relevance``.
    """

    kind = CharField(max_length=15, choices=GamePhotoFeedbackKind.choices)

    round = ForeignKey(
        "dashboard.GameRound",
        on_delete=CASCADE,
        related_name="photo_feedback",
    )
    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="spotguessr_photo_feedback",
    )

    if TYPE_CHECKING:
        round_id: int
        profile_id: int

    def __str__(self) -> str:
        return f"GamePhotoFeedback(round={self.round_id}, profile={self.profile_id}, kind={self.kind})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_spotguessr_photo_feedback"
        constraints = [
            UniqueConstraint(fields=["round", "profile"], name="db_sg_photo_feedback_unique"),
        ]


class GameSessionChatMessage(abstract.DashboardModel):
    """One chat message in a multiplayer session's live text chat (UL-392).

    Plain text, no E2EE - unlike DirectMessage/GroupMessage, session chat is
    ephemeral match banter between participants already visible to each
    other on the scoreboard, not a private conversation, so the
    ciphertext/key-exchange machinery those models carry buys nothing here.
    Sent and broadcast over ``GameSessionConsumer`` only; read history is
    served over HTTP for reconnects/late page-opens (see
    ``docs/designs/spotguessr.md``'s "Session chat").
    """

    body = CharField(max_length=1000)

    session = ForeignKey(
        "dashboard.GameSession",
        on_delete=CASCADE,
        related_name="chat_messages",
    )
    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="spotguessr_chat_messages",
    )

    if TYPE_CHECKING:
        session_id: int
        profile_id: int

    objects = GameSessionChatMessageManager()

    def __str__(self) -> str:
        return f"GameSessionChatMessage(session={self.session_id}, profile={self.profile_id})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_spotguessr_chat_messages"
        indexes = [
            Index(fields=["session", "created"], name="idxdb_sg_chat_session_created"),
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
