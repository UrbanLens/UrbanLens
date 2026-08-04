"""Consensus controller - gameplay, competitive lobby, and chat.

Session/round/answer/vote/lobby orchestration lives in
``services.consensus`` - this module only handles HTTP: request parsing,
participant/ownership checks, and JSON serialization. Mirrors
``controllers.spotguessr`` closely.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.gis.geos import Point
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views import View

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse

from urbanlens.dashboard.controllers.games import GAMES
from urbanlens.dashboard.models.consensus.model import (
    ConsensusAnswer,
    ConsensusFieldKind,
    ConsensusProfile,
    ConsensusRound,
    ConsensusRoundResolution,
    ConsensusSession,
    ConsensusSessionParticipant,
)
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.consensus import chat as consensus_chat, fields, serializers, session as consensus_session
from urbanlens.dashboard.services.social.connections import get_connections

logger = logging.getLogger(__name__)


def _current_profile(request: HttpRequest) -> Profile:
    profile, _ = Profile.objects.get_or_create(user=request.user)
    return profile


def _participant_session(profile: Profile, session_id: int) -> ConsensusSession:
    """The session, only if ``profile`` participates in it (any status) - 404 otherwise.

    404 (not 403) mirrors ``controllers.spotguessr``'s convention - a
    session another profile is playing shouldn't even reveal that it exists.
    """
    participant = ConsensusSessionParticipant.objects.filter(session_id=session_id, profile=profile).select_related("session").first()
    if participant is None:
        raise Http404("No such session for this profile.")
    return participant.session


def _joined_participant(profile: Profile, session: ConsensusSession) -> ConsensusSessionParticipant:
    participant = ConsensusSessionParticipant.objects.filter(session=session, profile=profile).first()
    if participant is None:
        raise Http404("No such session for this profile.")
    return participant


class ConsensusHomeView(LoginRequiredMixin, View):
    """The Consensus overview page: own points/level, friends, start-game form.

    GET /games/consensus/
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        profile = _current_profile(request)
        consensus_profile = ConsensusProfile.objects.get_or_create_for(profile)

        initial_session_id = None
        raw_session_id = request.GET.get("session")
        if raw_session_id and ConsensusSessionParticipant.objects.filter(session_id=raw_session_id, profile=profile).exists():
            initial_session_id = raw_session_id

        profile_summary = serializers.serialize_consensus_profile(consensus_profile)
        # points_for_next_level is the cumulative lifetime threshold for the next
        # level, not the amount still owed - which is what the HUD badge shows.
        points_to_next_level = max(0, profile_summary["points_for_next_level"] - profile_summary["total_points"])

        return render(
            request,
            "dashboard/pages/consensus/index.html",
            {
                "page_name": "consensus",
                # The shared games subnav and hero (partials/games/) read these.
                "games": GAMES,
                "game_stats": [
                    {"label": "Level", "value": profile_summary["level"], "note": "", "is_self": True},
                    {
                        "label": "Points",
                        "value": profile_summary["total_points"],
                        "note": f"{points_to_next_level} to next level",
                        "is_self": False,
                    },
                ],
                "consensus_profile": profile_summary,
                "min_rounds": consensus_session.MIN_ROUNDS_PER_SESSION,
                "max_rounds": consensus_session.MAX_ROUNDS_PER_SESSION,
                "default_rounds": consensus_session.DEFAULT_ROUNDS_PER_SESSION,
                "my_profile_id": profile.pk,
                "initial_session_id": initial_session_id,
            },
        )


class ConsensusFriendsView(LoginRequiredMixin, View):
    """The profile's friends, for the multiplayer invite picker.

    GET /games/consensus/friends/
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        profile = _current_profile(request)
        friends = get_connections(profile)
        return JsonResponse({"friends": [{"profile_id": friend.pk, "username": friend.username} for friend in friends]})


class ConsensusStartView(LoginRequiredMixin, View):
    """Start a new session - solo (immediately active) or competitive (a lobby to invite friends into).

    POST /games/consensus/start/   body: ``total_rounds``, optional ``invite_profile_ids`` (repeated).

    A solo start for a profile with no visited pins never creates a
    ``ConsensusSession`` - responds with ``{"error_code": "no_eligible_wikis"}``
    instead, matching ``SpotGuessrStartView``'s convention.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        profile = _current_profile(request)

        try:
            total_rounds = int(request.POST.get("total_rounds", consensus_session.DEFAULT_ROUNDS_PER_SESSION))
        except (TypeError, ValueError):
            total_rounds = consensus_session.DEFAULT_ROUNDS_PER_SESSION

        invite_ids = [pid for pid in request.POST.getlist("invite_profile_ids") if pid]

        if invite_ids:
            invitees = list(Profile.objects.filter(pk__in=invite_ids))
            try:
                game_session = consensus_session.start_competitive_session(profile, invitees, total_rounds=total_rounds)
            except consensus_session.ConsensusError as exc:
                return JsonResponse({"error": exc.safe_message}, status=400)
            return JsonResponse({"session_id": game_session.pk, "lobby": True, "session": serializers.serialize_session(game_session)})

        if not consensus_session.has_eligible_wikis([profile]):
            return JsonResponse({"error_code": "no_eligible_wikis"})

        try:
            game_session = consensus_session.start_solo_session(profile, total_rounds=total_rounds)
        except consensus_session.ConsensusError as exc:
            return JsonResponse({"error": exc.safe_message}, status=400)

        round_ = consensus_session.get_or_create_round(game_session)
        if round_ is None:
            consensus_session.complete_session(game_session)
            return JsonResponse({"error_code": "no_eligible_wikis"})

        return JsonResponse({"session_id": game_session.pk, "finished": False, "round": serializers.serialize_round(round_)})


class ConsensusLobbyView(LoginRequiredMixin, View):
    """The lobby's current state: status, and every participant (invited or joined).

    GET /games/consensus/session/<session_id>/lobby/
    """

    def get(self, request: HttpRequest, session_id: int) -> HttpResponse:
        profile = _current_profile(request)
        game_session = _participant_session(profile, session_id)
        return JsonResponse(serializers.serialize_session(game_session))


class ConsensusInviteView(LoginRequiredMixin, View):
    """Invite one more friend to a still-open lobby. Host-only.

    POST /games/consensus/session/<session_id>/invite/   body: ``profile_id``
    """

    def post(self, request: HttpRequest, session_id: int) -> HttpResponse:
        profile = _current_profile(request)
        game_session = _participant_session(profile, session_id)

        try:
            invitee = Profile.objects.get(pk=request.POST.get("profile_id"))
        except (Profile.DoesNotExist, ValueError, TypeError):
            return JsonResponse({"error": "profile_id is required."}, status=400)

        try:
            participant = consensus_session.invite_to_session(game_session, profile, invitee)
        except consensus_session.ConsensusError as exc:
            return JsonResponse({"error": exc.safe_message}, status=400)
        return JsonResponse({"participant": serializers.serialize_participant(participant)})


class ConsensusJoinView(LoginRequiredMixin, View):
    """Accept an invitation to a lobby.

    POST /games/consensus/session/<session_id>/join/
    """

    def post(self, request: HttpRequest, session_id: int) -> HttpResponse:
        profile = _current_profile(request)
        game_session = _participant_session(profile, session_id)

        try:
            participant = consensus_session.join_session(game_session, profile)
        except consensus_session.ConsensusError as exc:
            return JsonResponse({"error": exc.safe_message}, status=400)
        return JsonResponse({"participant": serializers.serialize_participant(participant)})


class ConsensusBeginView(LoginRequiredMixin, View):
    """Host starts the game: locks the roster and creates round 1.

    POST /games/consensus/session/<session_id>/begin/
    """

    def post(self, request: HttpRequest, session_id: int) -> HttpResponse:
        profile = _current_profile(request)
        game_session = _participant_session(profile, session_id)

        try:
            round_ = consensus_session.begin_session(game_session, profile)
        except consensus_session.ConsensusError as exc:
            return JsonResponse({"error": exc.safe_message}, status=400)

        if round_ is None:
            if consensus_session.rounds_played(game_session) == 0:
                return JsonResponse({"finished": False, "no_eligible_wikis": True})
            consensus_session.complete_session(game_session)
            return JsonResponse({"finished": True, "summary": consensus_session.session_summary(game_session)})
        return JsonResponse({"finished": False, "round": serializers.serialize_round(round_)})


class ConsensusEndSessionView(LoginRequiredMixin, View):
    """Host ends the game immediately - the manual escape hatch for a stalled or AFK competitive session.

    POST /games/consensus/session/<session_id>/end/
    """

    def post(self, request: HttpRequest, session_id: int) -> HttpResponse:
        profile = _current_profile(request)
        game_session = _participant_session(profile, session_id)

        try:
            consensus_session.end_session_now(game_session, profile)
        except consensus_session.ConsensusError as exc:
            return JsonResponse({"error": exc.safe_message}, status=400)
        return JsonResponse({"finished": True, "summary": consensus_session.session_summary(game_session)})


class ConsensusRoundView(LoginRequiredMixin, View):
    """The session's current round (for reloads/reconnects).

    GET /games/consensus/session/<session_id>/round/
    """

    def get(self, request: HttpRequest, session_id: int) -> HttpResponse:
        profile = _current_profile(request)
        game_session = _participant_session(profile, session_id)

        round_ = consensus_session.get_or_create_round(game_session)
        if round_ is None:
            if consensus_session.rounds_played(game_session) == 0:
                return JsonResponse({"finished": False, "no_eligible_wikis": True})
            consensus_session.complete_session(game_session)
            return JsonResponse({"finished": True, "summary": consensus_session.session_summary(game_session)})

        return JsonResponse({"finished": False, "round": serializers.serialize_round(round_)})


def _parse_answer_value(request: HttpRequest, round_: ConsensusRound) -> tuple[str | Point | None, JsonResponse | None]:
    """Parse a submitted answer body for ``round_``'s field kind.

    Returns:
        ``(value, None)`` on success, or ``(None, error_response)`` on failure.
    """
    if round_.field_kind == ConsensusFieldKind.PHOTO_COORDINATES:
        try:
            latitude = float(request.POST["latitude"])
            longitude = float(request.POST["longitude"])
        except (KeyError, TypeError, ValueError):
            return None, JsonResponse({"error": "latitude and longitude are required."}, status=400)
        return Point(longitude, latitude, srid=4326), None

    value = (request.POST.get("value") or "").strip()
    if not value:
        return None, JsonResponse({"error": "value is required."}, status=400)
    if not fields.validate_value(round_.field_kind, value):
        return None, JsonResponse({"error": "That's not a valid value for this field."}, status=400)
    return value, None


class ConsensusAnswerView(LoginRequiredMixin, View):
    """Submit an answer for the session's current round.

    POST /games/consensus/session/<session_id>/round/<round_id>/answer/
    body: ``value`` (text kinds) or ``latitude``/``longitude`` (PHOTO_COORDINATES)
    """

    def post(self, request: HttpRequest, session_id: int, round_id: int) -> HttpResponse:
        profile = _current_profile(request)
        game_session = _participant_session(profile, session_id)
        participant = _joined_participant(profile, game_session)
        if not participant.is_joined:
            return JsonResponse({"error": "Accept the invite before playing."}, status=403)

        round_ = get_object_or_404(ConsensusRound, pk=round_id, session=game_session)
        value, error = _parse_answer_value(request, round_)
        if error is not None:
            return error

        try:
            answer = consensus_session.submit_answer(round_, profile, value)
        except consensus_session.ConsensusError as exc:
            return JsonResponse({"error": exc.safe_message}, status=400)

        round_.refresh_from_db()
        return JsonResponse({"answer_id": answer.pk, "round": serializers.serialize_round(round_)})


class ConsensusSkipView(LoginRequiredMixin, View):
    """Skip the session's current round - "I don't know."

    POST /games/consensus/session/<session_id>/round/<round_id>/skip/
    """

    def post(self, request: HttpRequest, session_id: int, round_id: int) -> HttpResponse:
        profile = _current_profile(request)
        game_session = _participant_session(profile, session_id)
        participant = _joined_participant(profile, game_session)
        if not participant.is_joined:
            return JsonResponse({"error": "Accept the invite before playing."}, status=403)

        round_ = get_object_or_404(ConsensusRound, pk=round_id, session=game_session)
        try:
            consensus_session.skip_round(round_, profile)
        except consensus_session.ConsensusError as exc:
            return JsonResponse({"error": exc.safe_message}, status=400)

        round_.refresh_from_db()
        return JsonResponse({"round": serializers.serialize_round(round_)})


class ConsensusVoteView(LoginRequiredMixin, View):
    """Cast a vote during a competitive round's disagreement sub-phase.

    POST /games/consensus/session/<session_id>/round/<round_id>/vote/   body: ``answer_id``
    """

    def post(self, request: HttpRequest, session_id: int, round_id: int) -> HttpResponse:
        profile = _current_profile(request)
        game_session = _participant_session(profile, session_id)
        participant = _joined_participant(profile, game_session)
        if not participant.is_joined:
            return JsonResponse({"error": "Accept the invite before playing."}, status=403)

        round_ = get_object_or_404(ConsensusRound, pk=round_id, session=game_session)
        if round_.resolution != ConsensusRoundResolution.VOTE_OPEN:
            return JsonResponse({"error": "This round isn't open for voting."}, status=400)

        chosen_answer = get_object_or_404(ConsensusAnswer, pk=request.POST.get("answer_id"), round=round_)
        try:
            consensus_session.submit_vote(round_, profile, chosen_answer)
        except consensus_session.ConsensusError as exc:
            return JsonResponse({"error": exc.safe_message}, status=400)

        round_.refresh_from_db()
        return JsonResponse({"round": serializers.serialize_round(round_)})


class ConsensusPhotoUploadView(LoginRequiredMixin, View):
    """Upload a photo of the spot during a round - reuses the Memories upload pipeline.

    POST /games/consensus/session/<session_id>/round/<round_id>/photo/   body: an ``image`` file

    Attaches the photo to the round's wiki directly (an explicit "share this
    with the wiki" action, unlike an ordinary private pin-gallery upload) and
    awards a small bonus for helping a wiki that's short on photos.
    """

    def post(self, request: HttpRequest, session_id: int, round_id: int) -> HttpResponse:
        from urbanlens.dashboard.models.images.model import Image, MediaKind
        from urbanlens.dashboard.services.consensus import photos as consensus_photos, points as consensus_points
        from urbanlens.dashboard.services.core.celery import safely_enqueue_task
        from urbanlens.dashboard.services.media.images import compute_checksum, image_upload_error
        from urbanlens.dashboard.services.media.storage import per_profile_upload_lock, quota_error_for_upload
        from urbanlens.dashboard.tasks import process_image_upload

        profile = _current_profile(request)
        game_session = _participant_session(profile, session_id)
        participant = _joined_participant(profile, game_session)
        if not participant.is_joined:
            return JsonResponse({"error": "Accept the invite before playing."}, status=403)

        round_ = get_object_or_404(ConsensusRound, pk=round_id, session=game_session)
        image_file = request.FILES.get("image")
        if not image_file:
            return JsonResponse({"error": "No image provided."}, status=400)

        upload_error = image_upload_error(image_file, MediaKind.PHOTO)
        if upload_error:
            message, status = upload_error
            return JsonResponse({"error": message}, status=status)

        checksum = compute_checksum(image_file)
        if Image.objects.filter(profile=profile, checksum=checksum).exists():
            return JsonResponse({"error": "You already uploaded this file."}, status=409)

        with per_profile_upload_lock(profile):
            quota_error = quota_error_for_upload(profile, image_file.size)
            if quota_error:
                return JsonResponse({"error": quota_error}, status=413)
            image = Image.objects.create(
                image=image_file,
                profile=profile,
                wiki=round_.wiki,
                checksum=checksum,
                file_size=image_file.size,
                media_type=MediaKind.PHOTO,
            )

        safely_enqueue_task(process_image_upload, image.pk)
        consensus_photos.record_in_round_upload(round_, image, profile)
        consensus_points.award_points(profile.pk, consensus_points.PHOTO_UPLOAD_BONUS_POINTS, reason="photo_upload")
        return JsonResponse({"image_id": image.pk}, status=201)


class ConsensusChatHistoryView(LoginRequiredMixin, View):
    """Recent chat messages, for reconnects/late page-opens - live messages arrive over the WebSocket.

    GET /games/consensus/session/<session_id>/chat/
    """

    def get(self, request: HttpRequest, session_id: int) -> HttpResponse:
        profile = _current_profile(request)
        game_session = _participant_session(profile, session_id)
        messages = consensus_chat.recent_messages(game_session)
        return JsonResponse({"messages": [serializers.serialize_chat_message(message) for message in messages]})


class ConsensusSummaryView(LoginRequiredMixin, View):
    """The session's final scoreboard.

    GET /games/consensus/session/<session_id>/summary/
    """

    def get(self, request: HttpRequest, session_id: int) -> HttpResponse:
        profile = _current_profile(request)
        game_session = _participant_session(profile, session_id)
        return JsonResponse(consensus_session.session_summary(game_session))
