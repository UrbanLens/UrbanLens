"""Games hub - the site's directory of built-in games.

Currently SpotGuessr and Trivia; a future game only needs an entry in
``GAMES`` below - the landing page itself has no per-game logic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db.models import Model
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.views import View

from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.subscriptions import SiteFeature, user_has_feature
from urbanlens.dashboard.services.core.numbers import safe_int_or_none
from urbanlens.dashboard.services.social.connections import get_connections

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from django.db.models import QuerySet
    from django.http import HttpRequest, HttpResponse
    from django.http.response import HttpResponseBase

    from urbanlens.dashboard.services.core.session_access import SessionAccess


class AlphaFeatureRequiredMixin:
    """Refuses users who do not hold :attr:`SiteFeature.ALPHA_FEATURES`.

    The games (SpotGuessr, Trivia, Consensus) are alpha features: the same entitlement that hides the
    games nav and the hub must also gate every in-game route, otherwise anyone with a URL can play.
    Mix this in **after** ``LoginRequiredMixin`` (e.g. ``class FooView(LoginRequiredMixin,
    AlphaFeatureRequiredMixin, View)``) so anonymous visitors are redirected to the login page first
    rather than receiving a bare 403.

    Raises:
        PermissionDenied: When the authenticated user lacks the feature.
    """

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        if not user_has_feature(request.user, SiteFeature.ALPHA_FEATURES):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)


def participant_session_or_404[SessionT: Model](access: SessionAccess[SessionT], profile: Profile, session_id: int) -> SessionT:
    """The session, only if ``profile`` actively participates in it.

    404 rather than 403, so a session someone else is playing does not reveal that it exists.

    Raises:
        Http404: The session does not exist, or ``profile`` is not an active participant.
    """
    session = access.session_for(session_id, profile.pk)
    if session is None:
        raise Http404("No such session for this profile.")
    return session


def refuse_unless_joined(access: SessionAccess[Any], session: Model, profile: Profile) -> JsonResponse | None:
    """A 403 for an invitee who has not accepted, else None.

    Reading a round advances the game (it creates the next round, or completes the session), which only a player
    may do.
    """
    if access.is_joined_participant(session.pk, profile.pk):
        return None
    return JsonResponse({"error": "Accept the invite before playing."}, status=403)


def _is_id(raw: str) -> bool:
    return raw.isascii() and raw.isdigit() and len(raw) <= 18


def posted_invitees(request: HttpRequest, host: Profile) -> list[Profile] | None:
    """The friends of *host* a game's start request invites, from its repeated ``invite_profile_ids`` field.

    Returns:
        The profiles, empty for a solo start; None when any id is malformed or names no friend of *host*. An id
        nobody holds and one held by a stranger are refused after the same statement (P280).
    """
    raw_ids = [raw for raw in request.POST.getlist("invite_profile_ids") if raw]
    if not all(_is_id(raw) for raw in raw_ids):
        return None
    ids = {int(raw) for raw in raw_ids}
    invitees = list(Profile.objects.filter(Profile.connections_q(host), pk__in=ids))
    return invitees if len(invitees) == len(ids) else None


def posted_invitee(request: HttpRequest, host: Profile) -> Profile | JsonResponse:
    """The friend of *host* an invite request names in ``profile_id``, or the 400 to answer with.

    Call it only once the requester is known to be the host, or the answer tells a player who the host's friends
    are. An id nobody holds and one held by a stranger are refused after the same statement (P280).
    """
    invitee_id = safe_int_or_none(request.POST.get("profile_id"))
    if invitee_id is None:
        return JsonResponse({"error": "profile_id is required."}, status=400)
    invitee = Profile.objects.filter(Profile.connections_q(host), pk=invitee_id).first()
    return invitee if invitee is not None else JsonResponse({"error": "You can only invite friends."}, status=400)


def posted_player(request: HttpRequest, players: QuerySet[Any]) -> Profile | JsonResponse:
    """The player a kick request names in ``profile_id``, among a session's participant rows, or the 400 to answer with.

    An id nobody holds and one held by someone outside the session are refused after the same statement (P280).
    """
    player_id = safe_int_or_none(request.POST.get("profile_id"))
    if player_id is None:
        return JsonResponse({"error": "profile_id is required."}, status=400)
    player = Profile.objects.filter(pk=player_id, pk__in=players.values("profile_id")).first()
    return player if player is not None else JsonResponse({"error": "That profile is not part of this session."}, status=400)


def deep_link_session_id(access: SessionAccess[Any], profile: Profile, raw_session_id: str | None) -> int | None:
    """The ``?session=`` a game page should reopen on load, if ``profile`` still actively participates in it."""
    if not raw_session_id or not _is_id(raw_session_id):
        return None
    session_id = int(raw_session_id)
    return session_id if access.is_active_participant(session_id, profile.pk) else None


class GameEntry:
    """One row in the games directory.

    ``url`` resolves ``url_name`` lazily (on template access, not at import time) - ``GAMES`` below is
    built at module import, before every URL pattern is necessarily registered yet.
    """

    def __init__(self, *, name: str, description: str, icon: str, url_name: str) -> None:
        self.name = name
        self.description = description
        self.icon = icon
        self.url_name = url_name

    @property
    def url(self) -> str:
        return reverse(self.url_name)


GAMES = [
    GameEntry(
        name="SpotGuessr",
        description="Guess locations from photos, Street View, or other hints - solo or with friends.",
        icon="travel_explore",
        url_name="spotguessr",
    ),
    GameEntry(
        name="Trivia",
        description="Answer questions about the places you've pinned - solo or with friends.",
        icon="quiz",
        url_name="trivia",
    ),
    GameEntry(
        name="Consensus",
        description="Fill in missing wiki data for places you've visited - solo, or race friends to agree on the answer.",
        icon="fact_check",
        url_name="consensus",
    ),
]


#: Rating shown to a player who has never been rated in this game yet.
PROVISIONAL_RATING = 1500


class RatedRow(Protocol):
    """Any Glicko-2 rating row - ``PlayerModeRating``, ``PlayerTriviaRating``, ..."""

    @property
    def rating(self) -> float:
        """Display-scale rating, centered on 1500."""
        ...


def rating_stats(own_rating: RatedRow | None, friend_ratings: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Stat chips for the shared game hero (``partials/games/_game_hero_body.html``).

    SpotGuessr and Trivia expose structurally identical rating rows and identical
    ``visible_friend_ratings`` payloads, so both build their chips here rather than keeping two copies
    that can drift apart.

    Args:
        own_rating: The viewer's rating row, or None for a player with no rated games yet.
        friend_ratings: ``{"profile": Profile, "rating": <row> | None}`` mappings for friends who have
        opted into sharing (a friend who hasn't played yet still...

    Returns:
        One dict per chip, with ``label``, ``value``, ``note`` and ``is_self`` keys.
    """
    stats: list[dict[str, Any]] = [
        {
            "label": "Your rating",
            "value": round(own_rating.rating) if own_rating else PROVISIONAL_RATING,
            "note": "" if own_rating else "provisional",
            "is_self": True,
        },
    ]
    stats.extend(
        {
            "label": entry["profile"].username,
            "value": round(entry["rating"].rating) if entry.get("rating") else PROVISIONAL_RATING,
            "note": "",
            "is_self": False,
        }
        for entry in friend_ratings
    )
    return stats


class GamesOverviewView(LoginRequiredMixin, AlphaFeatureRequiredMixin, View):
    """The games hub: every built-in game.

    GET /games/
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        return render(request, "dashboard/pages/games/index.html", {"page_name": "games", "games": GAMES})


#: Bounds the work an ``exclude`` list can ask for; a lobby is far smaller.
_MAX_EXCLUDED_IDS = 200


def _parse_profile_ids(raw: str) -> set[int]:
    """The integer ids in a comma-separated list, ignoring anything else."""
    tokens = raw.split(",")[:_MAX_EXCLUDED_IDS]
    return {int(token) for token in (t.strip() for t in tokens) if token.isdecimal()}


class GameFriendPickerView(LoginRequiredMixin, AlphaFeatureRequiredMixin, View):
    """The profile's friends as invite checkboxes, for every game's invite picker.

    GET /games/friends/?exclude=<profile id>,<profile id>

    ``exclude`` drops profiles already in a lobby. It only shapes the list: each game's invite endpoint still checks
    that the invitee is a friend.
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        excluded = _parse_profile_ids(request.GET.get("exclude", ""))
        friends = sorted((friend for friend in get_connections(profile) if friend.pk not in excluded), key=lambda friend: friend.username.casefold())
        return render(request, "dashboard/partials/games/_friend_picker_options.html", {"friends": friends})
