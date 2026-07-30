"""Consensus models - the wiki-data-completion game.

Each round shows one player a single missing/unconfirmed piece of data about
a ``Wiki`` they've visited-pinned (a name, a description, an alias, a photo's
coordinates) and the player supplies it, applied to the wiki immediately -
see ``services.consensus.fields`` for the per-field-kind registry that drives
round generation and answer application. Solo sessions apply an answer the
instant it's submitted; the one competitive mode races participants and
resolves disagreement through a vote (or, failing that, a cross-session
tentative-answer pool) - see ``services.consensus.session``.

Points/leveling/trust live on a Consensus-only per-profile row
(``ConsensusProfile``), independent of SpotGuessr/Trivia's Glicko-2 ratings -
see ``services.consensus.points``/``trust`` for the formulas.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib.gis.db.models import PointField
from django.db.models import (
    CASCADE,
    SET_NULL,
    BooleanField,
    CharField,
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
from django.db.models.functions import Lower

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.consensus.queryset import (
    ConsensusAnswerManager,
    ConsensusProfileManager,
    ConsensusRoundManager,
    ConsensusRoundPhotoManager,
    ConsensusSessionChatMessageManager,
    ConsensusSessionManager,
    ConsensusSessionParticipantManager,
    ConsensusTentativeAnswerManager,
    ConsensusVoteManager,
)


class ConsensusFieldKind(abstract.TextChoices):
    """Which piece of Wiki/Image data a round is about.

    See ``services.consensus.fields``'s ``ConsensusFieldStrategy`` registry -
    this is the registry's key, the single place a new field kind is wired
    in (mirrors ``SpotGuessrMode``/``services.spotguessr.modes``).
    """

    WIKI_NAME = "wiki_name", "Place Name"
    WIKI_DESCRIPTION = "wiki_description", "Description"
    WIKI_INDOOR_OUTDOOR = "wiki_indoor_outdoor", "Indoor/Outdoor"
    WIKI_PIN_TYPE = "wiki_pin_type", "Place Type"
    WIKI_ALIAS = "wiki_alias", "Alias/Nickname"
    PHOTO_COORDINATES = "photo_coordinates", "Photo Location"


class ConsensusSessionStatus(abstract.TextChoices):
    """Lifecycle of a ConsensusSession - mirrors ``GameSessionStatus``.

    Solo sessions skip LOBBY entirely (created directly as ACTIVE with one
    JOINED participant). Competitive sessions start in LOBBY and only become
    ACTIVE when the host explicitly begins the game.
    """

    LOBBY = "lobby", "Lobby"
    ACTIVE = "active", "Active"
    COMPLETED = "completed", "Completed"
    ABANDONED = "abandoned", "Abandoned"


class ConsensusSessionParticipantStatus(abstract.TextChoices):
    """Whether a participant has accepted their invitation yet - mirrors ``GameSessionParticipantStatus``."""

    INVITED = "invited", "Invited"
    JOINED = "joined", "Joined"


class ConsensusRoundResolution(abstract.TextChoices):
    """How a round's answer(s) settled.

    ``PENDING`` covers both "solo, not yet answered" and "competitive, still
    collecting answers" - the single source of truth ``get_or_create_round``
    reads for "is this round done" (mirrors ``GameRound.revealed_at``, but
    named/valued so the competitive sub-phases have somewhere to live too).
    """

    PENDING = "pending", "Pending"
    AGREED = "agreed", "Agreed"
    VOTE_OPEN = "vote_open", "Vote Open"
    VOTE_RESOLVED = "vote_resolved", "Vote Resolved"
    TENTATIVE = "tentative", "Tentative"
    SKIPPED = "skipped", "Skipped"
    CHECK_PASSED = "check_passed", "Check Passed"
    CHECK_FAILED = "check_failed", "Check Failed"


class ConsensusTentativeStatus(abstract.TextChoices):
    """Lifecycle of a ConsensusTentativeAnswer - mirrors ``PinSuggestionStatus``."""

    PENDING = "pending", "Pending"
    APPLIED = "applied", "Applied"
    DISMISSED = "dismissed", "Dismissed"


class ConsensusProfile(abstract.DashboardModel):
    """A profile's Consensus points, level, and trust standing.

    Consensus-only - deliberately not shared with SpotGuessr/Trivia's
    Glicko-2 ratings, and not a generic cross-game Profile stat (per design
    decision - see the Consensus implementation plan).

    Attributes:
        total_points: Lifetime point total, from both in-game contributions
            and out-of-game manual wiki edits (see
            ``models.wiki_edit.signals``). See ``services.consensus.points``
            for the leveling formula this feeds.
        level: Cached level, recomputed whenever ``total_points`` changes
            (``services.consensus.points.award_points``) - avoids re-running
            the log-scale formula on every read.
        trust_alpha: Beta-Bernoulli posterior "successes" parameter for this
            profile's accuracy on trust-check rounds (see
            ``services.consensus.trust``). Starts at a weakly-informative
            prior, not zero - a brand-new player is neither trusted nor
            distrusted.
        trust_beta: Beta-Bernoulli posterior "failures" parameter - see
            ``trust_alpha``.
        checks_seen: How many trust-check rounds this profile has ever been
            shown, for display/debugging (the posterior itself is what
            actually drives trust, not this raw count).
        last_check_at: When this profile was last shown a trust-check round -
            enforces the minimum cooldown between checks so even a low-trust
            profile isn't checked on consecutive rounds.
        last_config: The player's last-used solo-mode settings, mirroring
            ``SpotGuessrPreference.last_config``.
    """

    #: Weakly-informative Beta(2, 2) prior - centered at 0.5 trust, so a
    #: single unlucky first check can't crater a new player's trust score.
    DEFAULT_TRUST_ALPHA = 2.0
    DEFAULT_TRUST_BETA = 2.0

    total_points = PositiveIntegerField(default=0)
    level = PositiveIntegerField(default=1)
    trust_alpha = FloatField(default=DEFAULT_TRUST_ALPHA)
    trust_beta = FloatField(default=DEFAULT_TRUST_BETA)
    checks_seen = PositiveIntegerField(default=0)
    last_check_at = DateTimeField(null=True, blank=True)
    last_config = JSONField(default=dict)

    profile = OneToOneField(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="consensus_profile",
    )

    if TYPE_CHECKING:
        profile_id: int

    objects = ConsensusProfileManager()

    @property
    def trust_score(self) -> float:
        """Posterior mean accuracy on trust-check rounds, in (0, 1)."""
        return self.trust_alpha / (self.trust_alpha + self.trust_beta)

    def __str__(self) -> str:
        return f"ConsensusProfile(profile={self.profile_id}, level={self.level}, points={self.total_points})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_consensus_profiles"


class ConsensusSession(abstract.DashboardModel):
    """One Consensus playthrough: a fixed round count, solo or competitive.

    No ``mode`` field - unlike SpotGuessr's fixed-per-session mode, a
    Consensus round's field kind varies round-to-round (see
    ``ConsensusRound.field_kind``); solo vs. competitive is expressed the
    same implicit way ``GameSession`` does it (a single-participant session
    created directly ACTIVE, vs. a multi-participant session starting in
    LOBBY).

    Attributes:
        status: Lifecycle state.
        config: Snapshot of the settings this session was started with -
            currently just ``vote_threshold`` (competitive-mode vote
            consensus share required, default 0.5). Snapshotted so a
            session's rules stay consistent even if defaults change mid-game.
        total_rounds: Number of rounds this session will play.
        host_profile: Who started the session.
    """

    DEFAULT_VOTE_THRESHOLD = 0.5

    status = CharField(max_length=12, choices=ConsensusSessionStatus.choices, default=ConsensusSessionStatus.ACTIVE)
    config = JSONField(default=dict)
    total_rounds = PositiveSmallIntegerField(default=5)
    started_at = DateTimeField(auto_now_add=True)
    ended_at = DateTimeField(null=True, blank=True)

    host_profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="hosted_consensus_sessions",
    )

    if TYPE_CHECKING:
        host_profile_id: int

    objects = ConsensusSessionManager()

    @property
    def is_active(self) -> bool:
        """Whether this session is still in progress."""
        return self.status == ConsensusSessionStatus.ACTIVE

    @property
    def vote_threshold(self) -> float:
        """The vote-consensus share required to resolve a competitive disagreement."""
        return float((self.config or {}).get("vote_threshold", self.DEFAULT_VOTE_THRESHOLD))

    def __str__(self) -> str:
        return f"ConsensusSession(host={self.host_profile_id}, status={self.status})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_consensus_sessions"


class ConsensusSessionParticipant(abstract.DashboardModel):
    """One profile's membership in a ConsensusSession, plus their running score.

    Attributes:
        status: INVITED until the profile accepts, then JOINED - mirrors
            ``GameSessionParticipant.status``. A solo session's host row is
            created directly JOINED.
        total_points_this_session: Denormalized running total, kept in sync
            by ``services.consensus.session`` as answers/votes resolve -
            session-scoped display only, distinct from the lifetime
            ``ConsensusProfile.total_points``.
        joined_at: When this row was created (set for INVITED rows too, same
            "really created_at" convention as ``GameSessionParticipant``).
    """

    status = CharField(max_length=10, choices=ConsensusSessionParticipantStatus.choices, default=ConsensusSessionParticipantStatus.JOINED)
    total_points_this_session = PositiveIntegerField(default=0)
    joined_at = DateTimeField(auto_now_add=True)

    session = ForeignKey(
        "dashboard.ConsensusSession",
        on_delete=CASCADE,
        related_name="participants",
    )
    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="consensus_participations",
    )

    if TYPE_CHECKING:
        session_id: int
        profile_id: int

    objects = ConsensusSessionParticipantManager()

    @property
    def is_joined(self) -> bool:
        """Whether this participant has actually accepted (vs. still just invited)."""
        return self.status == ConsensusSessionParticipantStatus.JOINED

    def __str__(self) -> str:
        return f"ConsensusSessionParticipant(session={self.session_id}, profile={self.profile_id}, status={self.status})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_consensus_session_participants"
        constraints = [
            UniqueConstraint(fields=["session", "profile"], name="db_consensus_participant_unique"),
        ]


class ConsensusRound(abstract.DashboardModel):
    """One missing piece of data to fill in within a session.

    Attributes:
        session: The session this round belongs to.
        sequence_index: 0-based position within the session's round order.
        wiki: The wiki this round's data belongs to.
        field_kind: Which field/data kind this round is about.
        target_image: The photo this round is about (``PHOTO_COORDINATES``
            only; null otherwise).
        is_check_round: True when this round's "missing" data is actually
            already known - a trust-check round (see
            ``services.consensus.trust``) - never surfaced to the client as
            anything other than an ordinary round.
        known_answer_snapshot: The real value, captured at round-creation
            time - only set when ``is_check_round`` is True, and never
            included in any outbound round payload (see
            ``services.consensus.serializers``).
        resolution: How this round's answer(s) settled - see
            ``ConsensusRoundResolution``.
        vote_opened_at: When the competitive-mode disagreement vote sub-phase
            began, for the vote-stall sweep.
        revealed_at: When this round's outcome became visible to participants.
    """

    sequence_index = PositiveSmallIntegerField()
    field_kind = CharField(max_length=20, choices=ConsensusFieldKind.choices)
    is_check_round = BooleanField(default=False)
    known_answer_snapshot = JSONField(null=True, blank=True)
    resolution = CharField(max_length=15, choices=ConsensusRoundResolution.choices, default=ConsensusRoundResolution.PENDING)
    vote_opened_at = DateTimeField(null=True, blank=True)
    revealed_at = DateTimeField(null=True, blank=True)

    session = ForeignKey(
        "dashboard.ConsensusSession",
        on_delete=CASCADE,
        related_name="rounds",
    )
    wiki = ForeignKey(
        "dashboard.Wiki",
        on_delete=CASCADE,
        related_name="consensus_rounds",
    )
    target_image = ForeignKey(
        "dashboard.Image",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="consensus_rounds",
    )

    if TYPE_CHECKING:
        session_id: int
        wiki_id: int
        target_image_id: int | None

    objects = ConsensusRoundManager()

    @property
    def is_settled(self) -> bool:
        """Whether this round has reached a final outcome (not still pending an answer or a vote)."""
        return self.resolution not in (ConsensusRoundResolution.PENDING, ConsensusRoundResolution.VOTE_OPEN)

    def __str__(self) -> str:
        return f"ConsensusRound(session={self.session_id}, sequence_index={self.sequence_index}, field_kind={self.field_kind})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_consensus_rounds"
        constraints = [
            UniqueConstraint(fields=["session", "sequence_index"], name="db_consensus_round_unique"),
        ]


class ConsensusAnswer(abstract.DashboardModel):
    """One participant's answer to one ConsensusRound.

    Attributes:
        text_value: Raw submitted text, for every field kind except
            ``PHOTO_COORDINATES``.
        normalized_text: ``text_value`` normalized via
            ``naming.normalize_name_for_comparison`` at submission time -
            what agreement/vote-clustering groups on for text kinds.
        guess_point: Submitted coordinates, ``PHOTO_COORDINATES`` only.
        is_correct_for_check: Set immediately when ``round.is_check_round`` -
            whether this answer matched the known value (feeds
            ``services.consensus.trust.record_check_result``).
        points_awarded: Points this specific answer earned, for the
            per-round reveal/summary UI.
    """

    text_value = CharField(max_length=500, null=True, blank=True)
    normalized_text = CharField(max_length=500, null=True, blank=True, db_index=True)
    guess_point = PointField(geography=True, srid=4326, null=True, blank=True)
    is_correct_for_check = BooleanField(null=True)
    points_awarded = PositiveIntegerField(default=0)
    submitted_at = DateTimeField(auto_now_add=True)

    round = ForeignKey(
        "dashboard.ConsensusRound",
        on_delete=CASCADE,
        related_name="answers",
    )
    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="consensus_answers",
    )

    if TYPE_CHECKING:
        round_id: int
        profile_id: int

    objects = ConsensusAnswerManager()

    def __str__(self) -> str:
        return f"ConsensusAnswer(round={self.round_id}, profile={self.profile_id})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_consensus_answers"
        constraints = [
            UniqueConstraint(fields=["round", "profile"], name="db_consensus_answer_unique"),
        ]


class ConsensusVote(abstract.DashboardModel):
    """One participant's vote, during a competitive round's disagreement sub-phase.

    ``chosen_answer`` points at one of the round's distinct submitted
    ``ConsensusAnswer`` rows - not necessarily the voter's own.
    """

    round = ForeignKey(
        "dashboard.ConsensusRound",
        on_delete=CASCADE,
        related_name="votes",
    )
    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="consensus_votes",
    )
    chosen_answer = ForeignKey(
        "dashboard.ConsensusAnswer",
        on_delete=CASCADE,
        related_name="votes_for",
    )

    if TYPE_CHECKING:
        round_id: int
        profile_id: int
        chosen_answer_id: int

    objects = ConsensusVoteManager()

    def __str__(self) -> str:
        return f"ConsensusVote(round={self.round_id}, profile={self.profile_id}, chosen_answer={self.chosen_answer_id})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_consensus_votes"
        constraints = [
            UniqueConstraint(fields=["round", "profile"], name="db_consensus_vote_unique"),
        ]


class ConsensusTentativeAnswer(abstract.DashboardModel):
    """A wiki-scoped, not-yet-applied candidate answer awaiting cross-session consensus.

    The Wiki-scoped analog of ``PinSuggestion`` (which is Pin-scoped and
    doesn't fit here): created when a competitive round's disagreement vote
    fails to reach consensus (see ``services.consensus.session``'s
    ``TENTATIVE`` resolution branch). ``support_count`` accumulates across
    separate sessions proposing the same value, so consensus can build up
    over time without ever touching the wiki until it does.

    Attributes:
        support_count: How many separate proposals (across sessions) have
            matched this value - bumped, not duplicated, when a later round
            proposes the same (normalized-equal, or within-distance-
            threshold-for-coordinates) value.
        status: PENDING while still awaiting consensus; APPLIED once a
            human/process applies it to the wiki; DISMISSED if rejected.
        source_round: The round that first created this row - attribution
            only.
    """

    text_value = CharField(max_length=500, null=True, blank=True)
    normalized_text = CharField(max_length=500, null=True, blank=True, db_index=True)
    guess_point = PointField(geography=True, srid=4326, null=True, blank=True)
    support_count = PositiveIntegerField(default=1)
    status = CharField(max_length=10, choices=ConsensusTentativeStatus.choices, default=ConsensusTentativeStatus.PENDING)
    first_suggested_at = DateTimeField(auto_now_add=True)
    last_suggested_at = DateTimeField(auto_now=True)

    wiki = ForeignKey(
        "dashboard.Wiki",
        on_delete=CASCADE,
        related_name="consensus_tentative_answers",
    )
    field_kind = CharField(max_length=20, choices=ConsensusFieldKind.choices)
    target_image = ForeignKey(
        "dashboard.Image",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="consensus_tentative_answers",
    )
    source_round = ForeignKey(
        "dashboard.ConsensusRound",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="tentative_answers_created",
    )

    if TYPE_CHECKING:
        wiki_id: int
        target_image_id: int | None
        source_round_id: int | None

    objects = ConsensusTentativeAnswerManager()

    def __str__(self) -> str:
        return f"ConsensusTentativeAnswer(wiki={self.wiki_id}, field_kind={self.field_kind}, support_count={self.support_count})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_consensus_tentative_answers"
        indexes = [
            Index(fields=["wiki", "field_kind"], name="idxdb_ctent_wiki_field"),
        ]
        constraints = [
            # Case-insensitive per (wiki, field_kind) - mirrors WikiAlias's
            # uniqueness rule. NULL text_value rows (PHOTO_COORDINATES) never
            # collide under this constraint - proximity-based dedup for those
            # is handled in services.consensus.tentative instead.
            UniqueConstraint(Lower("text_value"), "wiki", "field_kind", name="db_consensus_tentative_unique"),
        ]


class ConsensusRoundPhoto(abstract.DashboardModel):
    """Records that ``image`` was captured during ``round`` by ``profile``.

    A thin join, not a new FK on ``Image`` itself - keeps ``Image`` from
    needing to know about Consensus. Used for the round-resolution UI and
    the in-round photo-upload point bonus.
    """

    round = ForeignKey(
        "dashboard.ConsensusRound",
        on_delete=CASCADE,
        related_name="photos",
    )
    image = ForeignKey(
        "dashboard.Image",
        on_delete=CASCADE,
        related_name="consensus_round_photos",
    )
    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="consensus_round_photos",
    )

    if TYPE_CHECKING:
        round_id: int
        image_id: int
        profile_id: int

    objects = ConsensusRoundPhotoManager()

    def __str__(self) -> str:
        return f"ConsensusRoundPhoto(round={self.round_id}, image={self.image_id})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_consensus_round_photos"


class ConsensusSessionChatMessage(abstract.DashboardModel):
    """One chat message in a competitive session's live text chat - mirrors ``GameSessionChatMessage``."""

    body = CharField(max_length=1000)

    session = ForeignKey(
        "dashboard.ConsensusSession",
        on_delete=CASCADE,
        related_name="chat_messages",
    )
    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="consensus_chat_messages",
    )

    if TYPE_CHECKING:
        session_id: int
        profile_id: int

    objects = ConsensusSessionChatMessageManager()

    def __str__(self) -> str:
        return f"ConsensusSessionChatMessage(session={self.session_id}, profile={self.profile_id})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_consensus_chat_messages"
        indexes = [
            Index(fields=["session", "created"], name="idxdb_cons_chat_sesh_creat"),
        ]
