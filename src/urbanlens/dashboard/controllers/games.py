"""Games hub - the site's directory of built-in games.

Currently SpotGuessr and Trivia; a future game only needs an entry in
``GAMES`` below - the landing page itself has no per-game logic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.shortcuts import render
from django.urls import reverse
from django.views import View

from urbanlens.dashboard.models.subscriptions import SiteFeature, user_has_feature

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from django.http import HttpRequest, HttpResponse
    from django.http.response import HttpResponseBase


class AlphaFeatureRequiredMixin:
    """Refuses users who do not hold :attr:`SiteFeature.ALPHA_FEATURES`.

    The games (SpotGuessr, Trivia, Consensus) are alpha features: the same
    entitlement that hides the games nav and the hub must also gate every
    in-game route, otherwise anyone with a URL can play.

    Mix this in **after** ``LoginRequiredMixin`` (e.g.
    ``class FooView(LoginRequiredMixin, AlphaFeatureRequiredMixin, View)``)
    so anonymous visitors are redirected to the login page first rather than
    receiving a bare 403.

    Raises:
        PermissionDenied: When the authenticated user lacks the feature.
    """

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        if not user_has_feature(request.user, SiteFeature.ALPHA_FEATURES):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)


class GameEntry:
    """One row in the games directory.

    ``url`` resolves ``url_name`` lazily (on template access, not at import
    time) - ``GAMES`` below is built at module import, before every URL
    pattern is necessarily registered yet.
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

    SpotGuessr and Trivia expose structurally identical rating rows and
    identical ``visible_friend_ratings`` payloads, so both build their chips
    here rather than keeping two copies that can drift apart.

    Args:
        own_rating: The viewer's rating row, or None for a player with no rated games yet.
        friend_ratings: ``{"profile": Profile, "rating": <row> | None}`` mappings for
            friends who have opted into sharing (a friend who hasn't played yet
            still appears, with ``rating=None``).

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
