"""Trivia models - Glicko-2 ratings, question bank, sessions, rounds, answers, and votes.

See ``docs/prompts/todo.md``'s Trivia entry for the full spec this schema encodes:
locations drawn only from a pool every session participant can access (mirrors
SpotGuessr's eligibility rule), questions from three sources (user-submitted,
AI-generated from wiki articles, deterministic from structured location data),
a Glicko-2 player-skill/question-difficulty pairing (mirrors
``models.spotguessr``'s player/location pairing), and upvote/downvote/report
voting with a small passive "shown, no reaction" default.

Phase 1 built the full multiplayer-capable schema (sessions/participants
support LOBBY/INVITED from day one) but only ever created solo sessions;
Phase 2 adds ``TriviaSessionChatMessage`` and wires up the multiplayer
lifecycle in ``services.trivia.session``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

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
    Q,
    TextField,
)
from django.db.models.constraints import UniqueConstraint

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.spotguessr.model import _Glicko2RatingFields
from urbanlens.dashboard.models.trivia.queryset import (
    PlayerTriviaRatingManager,
    TriviaAnswerManager,
    TriviaQuestionManager,
    TriviaQuestionRatingManager,
    TriviaQuestionVoteManager,
    TriviaRoundManager,
    TriviaSessionChatMessageManager,
    TriviaSessionManager,
    TriviaSessionParticipantManager,
)

#: Glicko-2 defaults - same values as ``models.spotguessr``, reusing the shared
#: ``_Glicko2RatingFields`` mixin from that module rather than redefining them.
_DEFAULT_MU = 0.0
_DEFAULT_PHI = 350.0 / 173.7178
_DEFAULT_SIGMA = 0.06


class TriviaQuestionSource(abstract.TextChoices):
    """Where a TriviaQuestion came from."""

    USER_SUBMITTED = "user_submitted", "User Submitted"
    AI_GENERATED = "ai_generated", "AI Generated"
    DETERMINISTIC = "deterministic", "Deterministic"


class TriviaQuestionStatus(abstract.TextChoices):
    """Content-moderation lifecycle of a TriviaQuestion.

    Mirrors ``PinSuggestionStatus``'s shape. Deterministic questions are
    created ``APPROVED`` directly - they're template-generated from
    structured data, not free text, so they carry none of the person/
    bullying/in-group risk the classifier exists to catch (Phase 3 only
    classifies ``USER_SUBMITTED``/``AI_GENERATED`` questions).
    """

    PENDING_REVIEW = "pending_review", "Pending Review"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"


class TriviaQuestion(abstract.DashboardModel):
    """One trivia question about a location.

    Attributes:
        location: The location this question is about.
        prompt: The question text shown to players.
        answer: The canonical, display-cased accepted answer.
        answer_normalized: ``answer`` lowercased with non-alphanumeric
            characters stripped, computed in ``save()`` - what an incoming
            answer's own normalized form is exact-compared against.
        source: Where this question came from.
        status: Moderation lifecycle. Never surfaced to ``submitted_by`` -
            see the Trivia spec's "no feedback loop" requirement.
        submitted_by: The profile that wrote this question, for
            ``USER_SUBMITTED`` rows only. ``SET_NULL`` (not ``CASCADE``) once
            a question is approved it's shared community content, like a
            wiki edit - deleting the submitter's account shouldn't delete it.
        rejection_reason: Internal-only classifier output, never shown to
            ``submitted_by``.
        dedupe_key: Non-empty only for ``DETERMINISTIC`` questions - lets the
            generator be idempotent per ``(location, dedupe_key)`` instead of
            re-creating the same question every time it's called.
        wiki_incorporated_at: Set once an AI writing agent has folded this
            question into the location's wiki article (Phase 4), so it's
            never proposed a second time.
    """

    prompt = TextField()
    answer = CharField(max_length=255)
    answer_normalized = CharField(max_length=255, blank=True, db_index=True)
    source = CharField(max_length=20, choices=TriviaQuestionSource.choices)
    status = CharField(max_length=20, choices=TriviaQuestionStatus.choices, default=TriviaQuestionStatus.APPROVED)
    rejection_reason = CharField(max_length=255, null=True, blank=True)
    dedupe_key = CharField(max_length=100, blank=True, default="")
    wiki_incorporated_at = DateTimeField(null=True, blank=True)

    location = ForeignKey(
        "dashboard.Location",
        on_delete=CASCADE,
        related_name="trivia_questions",
    )
    submitted_by = ForeignKey(
        "dashboard.Profile",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="trivia_questions_submitted",
    )

    if TYPE_CHECKING:
        location_id: int
        submitted_by_id: int | None

    objects = TriviaQuestionManager()

    @staticmethod
    def normalize_answer(text: str) -> str:
        """Lowercase and strip everything but letters/digits, for exact-match comparison."""
        return "".join(char for char in text.casefold() if char.isalnum())

    def save(self, *args, **kwargs) -> None:
        """Keep ``answer_normalized`` in sync with ``answer`` on every save."""
        self.answer_normalized = self.normalize_answer(self.answer)
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"TriviaQuestion(location={self.location_id}, source={self.source}, status={self.status})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_trivia_questions"
        constraints = [
            UniqueConstraint(fields=["location", "dedupe_key"], condition=~Q(dedupe_key=""), name="db_trivia_question_dedupe_unique"),
        ]


class TriviaQuestionVoteKind(abstract.TextChoices):
    """What a participant did (or didn't do) about a question they were asked.

    Exact mirror of ``models.spotguessr.GamePhotoFeedbackKind`` - see
    ``services.trivia.voting`` for how these are weighted into a question's
    effective score.
    """

    UPVOTE = "upvote", "Upvote"
    DOWNVOTE = "downvote", "Downvote"
    REPORT = "report", "Report"
    NO_REACTION = "no_reaction", "No Reaction"


class TriviaQuestionVote(abstract.DashboardModel):
    """One participant's reaction to one TriviaQuestion.

    An event log, not a single mutable counter - the same profile can be
    asked the same question again in a later session and gets a fresh
    ``NO_REACTION`` backfill each time, since that signal is only meaningful
    in aggregate over many plays. An explicit vote always overwrites
    whatever was previously recorded for this ``(question, profile)`` pair -
    see ``services.trivia.voting.record_vote``/``backfill_no_reaction``.
    """

    kind = CharField(max_length=15, choices=TriviaQuestionVoteKind.choices)

    question = ForeignKey(
        "dashboard.TriviaQuestion",
        on_delete=CASCADE,
        related_name="votes",
    )
    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="trivia_question_votes",
    )

    if TYPE_CHECKING:
        question_id: int
        profile_id: int

    objects = TriviaQuestionVoteManager()

    def __str__(self) -> str:
        return f"TriviaQuestionVote(question={self.question_id}, profile={self.profile_id}, kind={self.kind})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_trivia_question_votes"
        constraints = [
            UniqueConstraint(fields=["question", "profile"], name="db_trivia_vote_unique"),
        ]


class PlayerTriviaRating(_Glicko2RatingFields, abstract.DashboardModel):
    """A profile's overall Glicko-2 skill rating for Trivia.

    One row per profile - unlike SpotGuessr's per-mode ratings, Trivia has
    no notion of separate "modes" to split skill across. Updated once per
    round played (``services.trivia.ratings.apply_round_ratings``), treating
    the round's question as the round's sole "opponent."
    """

    mu = FloatField(default=_DEFAULT_MU)
    phi = FloatField(default=_DEFAULT_PHI)
    sigma = FloatField(default=_DEFAULT_SIGMA)
    games_played = PositiveIntegerField(default=0)
    last_played_at = DateTimeField(null=True, blank=True)

    profile = OneToOneField(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="trivia_rating",
    )

    if TYPE_CHECKING:
        profile_id: int

    objects = PlayerTriviaRatingManager()

    def __str__(self) -> str:
        return f"PlayerTriviaRating(profile={self.profile_id}, rating={self.rating:.0f})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_trivia_player_ratings"


class TriviaQuestionRating(_Glicko2RatingFields, abstract.DashboardModel):
    """A question's Glicko-2 *difficulty* rating - the direct analog of SpotGuessr's LocationModeRating.

    One row per question. Updated once per round played, treating every
    participant in that round as an "opponent" with outcome score
    ``1 - (whether they answered correctly)`` - a question nobody can answer
    is "winning" against the field, exactly the high-difficulty signal a
    hard question should earn.
    """

    mu = FloatField(default=_DEFAULT_MU)
    phi = FloatField(default=_DEFAULT_PHI)
    sigma = FloatField(default=_DEFAULT_SIGMA)
    games_played = PositiveIntegerField(default=0)
    last_asked_at = DateTimeField(null=True, blank=True)

    question = OneToOneField(
        "dashboard.TriviaQuestion",
        on_delete=CASCADE,
        related_name="rating",
    )

    if TYPE_CHECKING:
        question_id: int

    objects = TriviaQuestionRatingManager()

    def __str__(self) -> str:
        return f"TriviaQuestionRating(question={self.question_id}, rating={self.rating:.0f})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_trivia_question_ratings"


class TriviaSessionStatus(abstract.TextChoices):
    """Lifecycle of a TriviaSession. Mirrors ``models.spotguessr.GameSessionStatus``."""

    LOBBY = "lobby", "Lobby"
    ACTIVE = "active", "Active"
    COMPLETED = "completed", "Completed"
    ABANDONED = "abandoned", "Abandoned"


class TriviaSessionParticipantStatus(abstract.TextChoices):
    """Whether a participant has accepted their invitation yet. Mirrors ``GameSessionParticipantStatus``."""

    INVITED = "invited", "Invited"
    JOINED = "joined", "Joined"


class TriviaSession(abstract.DashboardModel):
    """One Trivia playthrough: a config snapshot and a fixed round count.

    Modeled as a proper many-participant session from Phase 1, mirroring
    ``models.spotguessr.GameSession`` - every eligibility/scoring rule reads
    "all (joined) participants," not "the player," so a Phase 2 multiplayer
    lobby reuses these tables unchanged. Phase 1 only ever creates solo
    sessions (see ``services.trivia.session.start_solo_session``).

    Attributes:
        status: Lifecycle state.
        config: Snapshot of the settings this session was started with -
            ``difficulty`` (0.0-1.0 slider) and ``geo_bounds`` (a GeoJSON
            polygon/bbox, or None). Snapshotted so a session's rules stay
            consistent even if the host changes their defaults mid-game.
        total_rounds: Number of rounds this session will play.
        host_profile: Who started the session.
    """

    status = CharField(max_length=12, choices=TriviaSessionStatus.choices, default=TriviaSessionStatus.ACTIVE)
    config = JSONField(default=dict)
    total_rounds = PositiveSmallIntegerField(default=5)
    started_at = DateTimeField(auto_now_add=True)
    ended_at = DateTimeField(null=True, blank=True)

    host_profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="hosted_trivia_sessions",
    )

    if TYPE_CHECKING:
        host_profile_id: int

    objects = TriviaSessionManager()

    @property
    def is_active(self) -> bool:
        """Whether this session is still in progress."""
        return self.status == TriviaSessionStatus.ACTIVE

    def __str__(self) -> str:
        return f"TriviaSession(host={self.host_profile_id}, status={self.status})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_trivia_sessions"


class TriviaSessionParticipant(abstract.DashboardModel):
    """One profile's membership in a TriviaSession, plus their running score.

    ``total_points`` is a denormalized cache, kept in sync by
    ``services.trivia.session`` as answers are submitted, mirroring
    ``GameSessionParticipant.total_points``.
    """

    status = CharField(max_length=10, choices=TriviaSessionParticipantStatus.choices, default=TriviaSessionParticipantStatus.JOINED)
    total_points = PositiveIntegerField(default=0)
    joined_at = DateTimeField(auto_now_add=True)

    session = ForeignKey(
        "dashboard.TriviaSession",
        on_delete=CASCADE,
        related_name="participants",
    )
    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="trivia_participations",
    )

    if TYPE_CHECKING:
        session_id: int
        profile_id: int

    objects = TriviaSessionParticipantManager()

    @property
    def is_joined(self) -> bool:
        """Whether this participant has actually accepted (vs. still just invited)."""
        return self.status == TriviaSessionParticipantStatus.JOINED

    def __str__(self) -> str:
        return f"TriviaSessionParticipant(session={self.session_id}, profile={self.profile_id}, status={self.status})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_trivia_session_participants"
        constraints = [
            UniqueConstraint(fields=["session", "profile"], name="db_trivia_participant_unique"),
        ]


class TriviaRound(abstract.DashboardModel):
    """One question asked within a session.

    Attributes:
        session: The session this round belongs to.
        sequence_index: 0-based position within the session's round order.
        question: The question asked this round.
        revealed_at: When the answer became visible to at least one
            participant (immediately after answering, in solo play) - null
            until every joined participant has answered.
    """

    sequence_index = PositiveSmallIntegerField()
    revealed_at = DateTimeField(null=True, blank=True)

    session = ForeignKey(
        "dashboard.TriviaSession",
        on_delete=CASCADE,
        related_name="rounds",
    )
    question = ForeignKey(
        "dashboard.TriviaQuestion",
        on_delete=CASCADE,
        related_name="trivia_rounds",
    )

    if TYPE_CHECKING:
        session_id: int
        question_id: int

    objects = TriviaRoundManager()

    def __str__(self) -> str:
        return f"TriviaRound(session={self.session_id}, sequence_index={self.sequence_index}, question={self.question_id})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_trivia_rounds"
        constraints = [
            UniqueConstraint(fields=["session", "sequence_index"], name="db_trivia_round_unique"),
        ]


class TriviaAnswerMatchKind(abstract.TextChoices):
    """How an answer was judged correct/incorrect."""

    EXACT = "exact", "Exact Match"
    AI = "ai", "AI Judged"


class TriviaAnswer(abstract.DashboardModel):
    """One participant's answer to one TriviaRound.

    ``is_correct``/``points`` are computed once at submission time
    (``services.trivia.session.submit_answer``) and stored, matching
    ``Guess``'s "score once, never silently re-score" precedent.
    """

    raw_answer = CharField(max_length=500)
    is_correct = BooleanField()
    matched_via = CharField(max_length=10, choices=TriviaAnswerMatchKind.choices, default=TriviaAnswerMatchKind.EXACT)
    points = PositiveIntegerField(default=0)
    answered_at = DateTimeField(auto_now_add=True)

    round = ForeignKey(
        "dashboard.TriviaRound",
        on_delete=CASCADE,
        related_name="answers",
    )
    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="trivia_answers",
    )

    if TYPE_CHECKING:
        round_id: int
        profile_id: int

    objects = TriviaAnswerManager()

    def __str__(self) -> str:
        return f"TriviaAnswer(round={self.round_id}, profile={self.profile_id}, is_correct={self.is_correct})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_trivia_answers"
        constraints = [
            UniqueConstraint(fields=["round", "profile"], name="db_trivia_answer_unique"),
        ]


class TriviaSessionChatMessage(abstract.DashboardModel):
    """One chat message in a multiplayer session's live text chat. Mirrors ``GameSessionChatMessage``.

    Plain text, no E2EE - ephemeral match banter between participants
    already visible to each other on the scoreboard, not a private
    conversation. Sent and broadcast over ``TriviaSessionConsumer`` only;
    read history is served over HTTP for reconnects/late page-opens.
    """

    body = CharField(max_length=1000)

    session = ForeignKey(
        "dashboard.TriviaSession",
        on_delete=CASCADE,
        related_name="chat_messages",
    )
    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="trivia_chat_messages",
    )

    if TYPE_CHECKING:
        session_id: int
        profile_id: int

    objects = TriviaSessionChatMessageManager()

    def __str__(self) -> str:
        return f"TriviaSessionChatMessage(session={self.session_id}, profile={self.profile_id})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_trivia_chat_messages"
        indexes = [
            # Index names are capped at 30 characters (Oracle's historical
            # limit, which Django enforces universally regardless of the
            # active backend) - "trivia" is abbreviated to fit, mirroring
            # models.spotguessr's own "sg" abbreviation for the same reason.
            Index(fields=["session", "created"], name="idxdb_trivia_chat_created"),
        ]


class TriviaPreference(abstract.DashboardModel):
    """Per-profile Trivia settings - same shape as ``SpotGuessrPreference``.

    Attributes:
        show_ratings_to_friends: Whether this profile's rating may appear on
            a friend's Trivia overview page. Default True (opt-out).
        last_config: The player's last-used game settings (difficulty,
            geo bounds), so returning to the game doesn't reset the
            difficulty slider every time.
    """

    show_ratings_to_friends = BooleanField(default=True)
    last_config = JSONField(default=dict)

    profile = OneToOneField(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="trivia_preference",
    )

    if TYPE_CHECKING:
        profile_id: int

    def __str__(self) -> str:
        return f"TriviaPreference(profile={self.profile_id})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_trivia_preferences"
