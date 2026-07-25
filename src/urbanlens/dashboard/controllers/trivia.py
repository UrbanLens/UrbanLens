"""Trivia controller - solo gameplay (Phase 1).

See ``services.trivia.session`` for the orchestration this module only
adapts to HTTP: request parsing, participant/ownership checks, JSON
serialization. Multiplayer (lobby/invite/join/begin/chat) is a follow-up
phase, mirroring ``controllers.spotguessr``'s own solo-then-multiplayer
history.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views import View

from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trivia.model import (
    PlayerTriviaRating,
    TriviaAnswer,
    TriviaPreference,
    TriviaQuestion,
    TriviaRound,
    TriviaSession,
    TriviaSessionParticipant,
)
from urbanlens.dashboard.services.trivia import eligibility, session as trivia_session, voting

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse


def _current_profile(request: HttpRequest) -> Profile:
    profile, _ = Profile.objects.get_or_create(user=request.user)
    return profile


def _participant_session(profile: Profile, session_id: int) -> TriviaSession:
    """The session, only if ``profile`` participates in it (any status) - 404 otherwise.

    404 (not 403) mirrors ``controllers.spotguessr``'s convention: a session
    another profile is playing shouldn't even reveal that it exists.
    """
    participant = TriviaSessionParticipant.objects.filter(session_id=session_id, profile=profile).select_related("session").first()
    if participant is None:
        raise Http404("No such session for this profile.")
    return participant.session


#: Placeholder ids reversed into the URL templates handed to the frontend -
#: mirrors ``controllers.spotguessr``'s ``_url_templates``, needed because
#: ``{% url %}`` can't emit a JS template directly for ``<int:...>`` converters.
_SESSION_ID_SENTINEL = 999999999
_ROUND_ID_SENTINEL = 888888888
_QUESTION_ID_SENTINEL = 777777777


def _url_templates() -> dict[str, str]:
    """Every Trivia endpoint the frontend needs, with numeric-id placeholders for the parameterized ones."""
    session_kwargs = {"session_id": _SESSION_ID_SENTINEL}
    return {
        "start": reverse("trivia.start"),
        "round": reverse("trivia.round", kwargs=session_kwargs),
        "answer": reverse("trivia.answer", kwargs={"session_id": _SESSION_ID_SENTINEL, "round_id": _ROUND_ID_SENTINEL}),
        "summary": reverse("trivia.summary", kwargs=session_kwargs),
        "vote": reverse("trivia.vote", kwargs={"question_id": _QUESTION_ID_SENTINEL}),
        "session_id_sentinel": str(_SESSION_ID_SENTINEL),
        "round_id_sentinel": str(_ROUND_ID_SENTINEL),
        "question_id_sentinel": str(_QUESTION_ID_SENTINEL),
    }


def _serialize_round(round_: TriviaRound) -> dict:
    """A round's public shape - never includes the answer."""
    return {
        "round_id": round_.pk,
        "question_id": round_.question_id,
        "sequence_index": round_.sequence_index,
        "prompt": round_.question.prompt,
        "revealed_at": round_.revealed_at.isoformat() if round_.revealed_at else None,
    }


def _serialize_reveal(round_: TriviaRound, answer: TriviaAnswer) -> dict:
    """One guesser's own reveal - includes the answer only once the round is revealed."""
    payload: dict[str, object] = {
        "round_id": round_.pk,
        "question_id": round_.question_id,
        "is_correct": answer.is_correct,
        "points": answer.points,
    }
    if round_.revealed_at is not None:
        payload["answer"] = round_.question.answer
    return payload


class TriviaHomeView(LoginRequiredMixin, View):
    """The Trivia overview page: own rating, last-used settings, start-game form.

    GET /games/trivia/
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        profile = _current_profile(request)
        preference = getattr(profile, "trivia_preference", None)
        own_rating = PlayerTriviaRating.objects.filter(profile=profile).first()

        return render(
            request,
            "dashboard/pages/trivia/index.html",
            {
                "page_name": "trivia",
                "own_rating": own_rating,
                "last_config": preference.last_config if preference else {},
                "min_rounds": trivia_session.MIN_ROUNDS_PER_SESSION,
                "max_rounds": trivia_session.MAX_ROUNDS_PER_SESSION,
                "default_rounds": trivia_session.DEFAULT_ROUNDS_PER_SESSION,
                "urls": _url_templates(),
            },
        )


class TriviaStartView(LoginRequiredMixin, View):
    """Start a new solo session.

    POST /games/trivia/start/   body: ``difficulty``, ``total_rounds``

    A start whose config has no eligible questions at all (e.g. the profile
    hasn't pinned anything with an in-rotation question yet) never creates a
    TriviaSession - it responds with ``{"error_code": "no_eligible_questions"}``
    instead, mirroring ``SpotGuessrStartView``'s solo pre-check.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        profile = _current_profile(request)

        try:
            difficulty = float(request.POST.get("difficulty", 0.5))
        except (TypeError, ValueError):
            return JsonResponse({"error": "difficulty must be a number between 0 and 1."}, status=400)

        try:
            total_rounds = int(request.POST.get("total_rounds", trivia_session.DEFAULT_ROUNDS_PER_SESSION))
        except (TypeError, ValueError):
            total_rounds = trivia_session.DEFAULT_ROUNDS_PER_SESSION

        config = trivia_session.TriviaConfig(difficulty=difficulty)

        preference, _ = TriviaPreference.objects.get_or_create(profile=profile)
        preference.last_config = config.to_dict()
        preference.save(update_fields=["last_config", "updated"])

        if not eligibility.has_eligible_questions([profile]):
            return JsonResponse({"error_code": "no_eligible_questions"})

        game_session = trivia_session.start_solo_session(profile, config, total_rounds=total_rounds)
        round_ = trivia_session.get_or_create_round(game_session)
        if round_ is None:
            trivia_session.complete_session(game_session)
            return JsonResponse({"session_id": game_session.pk, "finished": True, "summary": trivia_session.session_summary(game_session)})

        return JsonResponse({"session_id": game_session.pk, "finished": False, "round": _serialize_round(round_)})


class TriviaRoundView(LoginRequiredMixin, View):
    """The session's current round (for reloads/reconnects).

    GET /games/trivia/session/<session_id>/round/
    """

    def get(self, request: HttpRequest, session_id: int) -> HttpResponse:
        profile = _current_profile(request)
        game_session = _participant_session(profile, session_id)

        round_ = trivia_session.get_or_create_round(game_session)
        if round_ is None:
            if trivia_session.rounds_played(game_session) == 0:
                return JsonResponse({"finished": False, "no_eligible_questions": True})
            trivia_session.complete_session(game_session)
            return JsonResponse({"finished": True, "summary": trivia_session.session_summary(game_session)})

        return JsonResponse({"finished": False, "round": _serialize_round(round_)})


class TriviaAnswerView(LoginRequiredMixin, View):
    """Submit an answer for the session's current round.

    POST /games/trivia/session/<session_id>/round/<round_id>/answer/   body: ``answer``
    """

    def post(self, request: HttpRequest, session_id: int, round_id: int) -> HttpResponse:
        profile = _current_profile(request)
        game_session = _participant_session(profile, session_id)
        round_ = get_object_or_404(TriviaRound, pk=round_id, session=game_session)

        raw_answer = request.POST.get("answer", "")
        if not raw_answer.strip():
            return JsonResponse({"error": "answer is required."}, status=400)

        try:
            answer = trivia_session.submit_answer(round_, profile, raw_answer)
        except trivia_session.TriviaError as exc:
            return JsonResponse({"error": str(exc)}, status=400)

        round_.refresh_from_db()
        return JsonResponse(_serialize_reveal(round_, answer))


class TriviaSummaryView(LoginRequiredMixin, View):
    """The session's final scoreboard.

    GET /games/trivia/session/<session_id>/summary/
    """

    def get(self, request: HttpRequest, session_id: int) -> HttpResponse:
        profile = _current_profile(request)
        game_session = _participant_session(profile, session_id)
        return JsonResponse(trivia_session.session_summary(game_session))


class TriviaQuestionVoteView(LoginRequiredMixin, View):
    """Upvote, downvote, or report the question just answered. Host-agnostic - any participant may vote.

    POST /games/trivia/questions/<question_id>/vote/   body: ``kind``

    Restricted to a question the profile has actually been asked at least
    once, mirroring ``SpotGuessrPhotoFeedbackView``'s "you can only react to
    a round you've guessed on" rule.
    """

    def post(self, request: HttpRequest, question_id: int) -> HttpResponse:
        profile = _current_profile(request)
        question = get_object_or_404(TriviaQuestion, pk=question_id)

        kind = request.POST.get("kind")
        if kind not in voting.EXPLICIT_KINDS:
            return JsonResponse({"error": f"kind must be one of {', '.join(voting.EXPLICIT_KINDS)}."}, status=400)
        if not TriviaAnswer.objects.filter(round__question=question, profile=profile).exists():
            return JsonResponse({"error": "You can only vote on a question you've answered."}, status=403)

        vote = voting.record_vote(question, profile, kind)
        return JsonResponse({"kind": vote.kind})
