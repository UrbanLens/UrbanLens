"""Trivia controller - solo and multiplayer gameplay, lobby, and chat.

See ``services.trivia.session`` for the orchestration this module only
adapts to HTTP: request parsing, participant/ownership checks, JSON
serialization (an answer is never serialized until it reveals it; real-time
fan-out to other participants happens over
``consumers.TriviaSessionConsumer``, not here). Mirrors
``controllers.spotguessr`` throughout.
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
from urbanlens.dashboard.services.social.connections import get_connections
from urbanlens.dashboard.services.trivia import chat as trivia_chat, eligibility, serializers, session as trivia_session, social, submission, voting

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
        "friends": reverse("trivia.friends"),
        "settings": reverse("trivia.settings"),
        "lobby": reverse("trivia.lobby", kwargs=session_kwargs),
        "invite": reverse("trivia.invite", kwargs=session_kwargs),
        "join": reverse("trivia.join", kwargs=session_kwargs),
        "begin": reverse("trivia.begin", kwargs=session_kwargs),
        "end": reverse("trivia.end", kwargs=session_kwargs),
        "leave": reverse("trivia.leave", kwargs=session_kwargs),
        "kick": reverse("trivia.kick", kwargs=session_kwargs),
        "round": reverse("trivia.round", kwargs=session_kwargs),
        "answer": reverse("trivia.answer", kwargs={"session_id": _SESSION_ID_SENTINEL, "round_id": _ROUND_ID_SENTINEL}),
        "chat_history": reverse("trivia.chat_history", kwargs=session_kwargs),
        "summary": reverse("trivia.summary", kwargs=session_kwargs),
        "vote": reverse("trivia.vote", kwargs={"question_id": _QUESTION_ID_SENTINEL}),
        "session_id_sentinel": str(_SESSION_ID_SENTINEL),
        "round_id_sentinel": str(_ROUND_ID_SENTINEL),
        "question_id_sentinel": str(_QUESTION_ID_SENTINEL),
    }


class TriviaHomeView(LoginRequiredMixin, View):
    """The Trivia overview page: own rating, last-used settings, start-game form.

    GET /games/trivia/
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        profile = _current_profile(request)
        preference = getattr(profile, "trivia_preference", None)
        own_rating = PlayerTriviaRating.objects.filter(profile=profile).first()

        raw_session_id = request.GET.get("session")
        initial_session_id = None
        if raw_session_id and TriviaSessionParticipant.objects.filter(session_id=raw_session_id, profile=profile).exists():
            initial_session_id = raw_session_id

        return render(
            request,
            "dashboard/pages/trivia/index.html",
            {
                "page_name": "trivia",
                "own_rating": own_rating,
                "friend_ratings": social.visible_friend_ratings(profile),
                "show_ratings_to_friends": preference.show_ratings_to_friends if preference else True,
                "last_config": preference.last_config if preference else {},
                "min_rounds": trivia_session.MIN_ROUNDS_PER_SESSION,
                "max_rounds": trivia_session.MAX_ROUNDS_PER_SESSION,
                "default_rounds": trivia_session.DEFAULT_ROUNDS_PER_SESSION,
                "urls": _url_templates(),
                "my_profile_id": profile.pk,
                "initial_session_id": initial_session_id,
            },
        )


class TriviaStartView(LoginRequiredMixin, View):
    """Start a new session - solo (immediately active) or multiplayer (a lobby to invite friends into).

    POST /games/trivia/start/   body: ``difficulty``, ``total_rounds``,
    optional ``invite_profile_ids`` (repeated) to start a multiplayer lobby
    instead of solo play.

    A solo start whose config has no eligible questions at all (e.g. the
    profile hasn't pinned anything with an in-rotation question yet) never
    creates a TriviaSession - it responds with
    ``{"error_code": "no_eligible_questions"}`` instead. Multiplayer can't be
    pre-checked this way (invitees haven't joined yet) - see
    ``TriviaBeginView`` for that case.
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
        invite_ids = [pid for pid in request.POST.getlist("invite_profile_ids") if pid]

        preference, _ = TriviaPreference.objects.get_or_create(profile=profile)
        preference.last_config = config.to_dict()
        preference.save(update_fields=["last_config", "updated"])

        if invite_ids:
            invitees = list(Profile.objects.filter(pk__in=invite_ids))
            try:
                game_session = trivia_session.start_multiplayer_session(profile, config, invitees, total_rounds=total_rounds)
            except trivia_session.TriviaError as exc:
                return JsonResponse({"error": exc.safe_message}, status=400)
            return JsonResponse({"session_id": game_session.pk, "lobby": True, "session": serializers.serialize_session(game_session)})

        if not eligibility.has_eligible_questions([profile]):
            return JsonResponse({"error_code": "no_eligible_questions"})

        game_session = trivia_session.start_solo_session(profile, config, total_rounds=total_rounds)
        round_ = trivia_session.get_or_create_round(game_session)
        if round_ is None:
            trivia_session.complete_session(game_session)
            return JsonResponse({"session_id": game_session.pk, "finished": True, "summary": trivia_session.session_summary(game_session)})

        return JsonResponse({"session_id": game_session.pk, "finished": False, "round": serializers.serialize_round(round_)})


class TriviaFriendsView(LoginRequiredMixin, View):
    """The profile's friends, for the multiplayer invite picker.

    GET /games/trivia/friends/
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        profile = _current_profile(request)
        friends = get_connections(profile)
        return JsonResponse({"friends": [{"profile_id": friend.pk, "username": friend.username} for friend in friends]})


class TriviaSettingsView(LoginRequiredMixin, View):
    """Update Trivia preferences.

    POST /games/trivia/settings/   body: ``show_ratings_to_friends`` ("on"/"off")
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        profile = _current_profile(request)
        preference, _ = TriviaPreference.objects.get_or_create(profile=profile)
        preference.show_ratings_to_friends = request.POST.get("show_ratings_to_friends") == "on"
        preference.save(update_fields=["show_ratings_to_friends", "updated"])
        return JsonResponse({"show_ratings_to_friends": preference.show_ratings_to_friends})


class TriviaLobbyView(LoginRequiredMixin, View):
    """The lobby's current state: status and every participant (invited or joined).

    GET /games/trivia/session/<session_id>/lobby/
    """

    def get(self, request: HttpRequest, session_id: int) -> HttpResponse:
        profile = _current_profile(request)
        game_session = _participant_session(profile, session_id)
        return JsonResponse(serializers.serialize_session(game_session))


class TriviaInviteView(LoginRequiredMixin, View):
    """Invite one more friend to a still-open lobby. Host-only.

    POST /games/trivia/session/<session_id>/invite/   body: ``profile_id``
    """

    def post(self, request: HttpRequest, session_id: int) -> HttpResponse:
        profile = _current_profile(request)
        game_session = _participant_session(profile, session_id)

        try:
            invitee = Profile.objects.get(pk=request.POST.get("profile_id"))
        except (Profile.DoesNotExist, ValueError, TypeError):
            return JsonResponse({"error": "profile_id is required."}, status=400)

        try:
            participant = trivia_session.invite_to_session(game_session, profile, invitee)
        except trivia_session.TriviaError as exc:
            return JsonResponse({"error": exc.safe_message}, status=400)
        return JsonResponse({"participant": serializers.serialize_participant(participant)})


class TriviaJoinView(LoginRequiredMixin, View):
    """Accept an invitation to a lobby.

    POST /games/trivia/session/<session_id>/join/
    """

    def post(self, request: HttpRequest, session_id: int) -> HttpResponse:
        profile = _current_profile(request)
        game_session = _participant_session(profile, session_id)

        try:
            participant = trivia_session.join_session(game_session, profile)
        except trivia_session.TriviaError as exc:
            return JsonResponse({"error": exc.safe_message}, status=400)
        return JsonResponse({"participant": serializers.serialize_participant(participant)})


class TriviaBeginView(LoginRequiredMixin, View):
    """Host starts the game: locks the roster and creates round 1.

    POST /games/trivia/session/<session_id>/begin/
    """

    def post(self, request: HttpRequest, session_id: int) -> HttpResponse:
        profile = _current_profile(request)
        game_session = _participant_session(profile, session_id)

        try:
            round_ = trivia_session.begin_session(game_session, profile)
        except trivia_session.TriviaError as exc:
            return JsonResponse({"error": exc.safe_message}, status=400)

        if round_ is None:
            if trivia_session.rounds_played(game_session) == 0:
                return JsonResponse({"finished": False, "no_eligible_questions": True})
            trivia_session.complete_session(game_session)
            return JsonResponse({"finished": True, "summary": trivia_session.session_summary(game_session)})
        return JsonResponse({"finished": False, "round": serializers.serialize_round(round_)})


class TriviaEndSessionView(LoginRequiredMixin, View):
    """Host ends the game immediately - the manual escape hatch for a stalled or AFK multiplayer session.

    POST /games/trivia/session/<session_id>/end/

    Unlike waiting out the stall-sweep Celery task
    (``tasks.sweep_stalled_trivia_sessions``), this lets the host end the
    game the moment they decide it's not going anywhere. Any in-flight round
    is revealed first with whatever answers already exist (see
    ``services.trivia.session.end_session_now``).
    """

    def post(self, request: HttpRequest, session_id: int) -> HttpResponse:
        profile = _current_profile(request)
        game_session = _participant_session(profile, session_id)

        try:
            trivia_session.end_session_now(game_session, profile)
        except trivia_session.TriviaError as exc:
            return JsonResponse({"error": exc.safe_message}, status=400)
        return JsonResponse({"finished": True, "summary": trivia_session.session_summary(game_session)})


class TriviaLeaveSessionView(LoginRequiredMixin, View):
    """The calling profile leaves this session - or declines an invitation to it.

    POST /games/trivia/session/<session_id>/leave/
    """

    def post(self, request: HttpRequest, session_id: int) -> HttpResponse:
        profile = _current_profile(request)
        game_session = _participant_session(profile, session_id)

        try:
            trivia_session.leave_session(game_session, profile)
        except trivia_session.TriviaError as exc:
            return JsonResponse({"error": exc.safe_message}, status=400)
        return JsonResponse({"left": True})


class TriviaKickParticipantView(LoginRequiredMixin, View):
    """Host removes another participant from the session. Host-only.

    POST /games/trivia/session/<session_id>/kick/   body: ``profile_id``
    """

    def post(self, request: HttpRequest, session_id: int) -> HttpResponse:
        profile = _current_profile(request)
        game_session = _participant_session(profile, session_id)

        try:
            target = Profile.objects.get(pk=request.POST.get("profile_id"))
        except (Profile.DoesNotExist, ValueError, TypeError):
            return JsonResponse({"error": "profile_id is required."}, status=400)

        try:
            trivia_session.kick_participant(game_session, profile, target)
        except trivia_session.TriviaError as exc:
            return JsonResponse({"error": exc.safe_message}, status=400)
        return JsonResponse({"kicked": True})


class TriviaChatHistoryView(LoginRequiredMixin, View):
    """Recent chat messages, for reconnects/late page-opens - live messages arrive over the WebSocket.

    GET /games/trivia/session/<session_id>/chat/
    """

    def get(self, request: HttpRequest, session_id: int) -> HttpResponse:
        profile = _current_profile(request)
        game_session = _participant_session(profile, session_id)
        messages = trivia_chat.recent_messages(game_session)
        return JsonResponse({"messages": [serializers.serialize_chat_message(message) for message in messages]})


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

        return JsonResponse({"finished": False, "round": serializers.serialize_round(round_)})


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
            return JsonResponse({"error": exc.safe_message}, status=400)

        round_.refresh_from_db()
        return JsonResponse(serializers.serialize_reveal(round_, answer))


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


class TriviaQuestionSubmitView(LoginRequiredMixin, View):
    """Submit a user-written trivia question about a location the profile has pinned.

    POST /games/trivia/questions/submit/   body: ``location_id``, ``prompt``, ``answer``

    The question is created PENDING_REVIEW and classified asynchronously -
    the response never indicates whether it will ultimately be
    approved or rejected (see services.trivia.classifier's module docstring
    for why the submitter is deliberately never told).
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.pin.model import Pin

        profile = _current_profile(request)

        try:
            location = Location.objects.get(pk=request.POST.get("location_id"))
        except (Location.DoesNotExist, ValueError, TypeError):
            return JsonResponse({"error": "location_id is required."}, status=400)
        if not Pin.objects.filter(profile=profile, location=location).exists():
            return JsonResponse({"error": "You can only submit questions about places you've pinned."}, status=403)

        prompt = request.POST.get("prompt", "").strip()
        answer = request.POST.get("answer", "").strip()
        if not prompt or not answer:
            return JsonResponse({"error": "prompt and answer are required."}, status=400)

        submission.submit_user_question(profile, location, prompt, answer)
        return JsonResponse({"submitted": True})
