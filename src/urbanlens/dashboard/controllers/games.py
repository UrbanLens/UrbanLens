"""Games hub - the site's directory of built-in games.

Currently just SpotGuessr; a future game only needs an entry in ``GAMES``
below - the landing page itself has no per-game logic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import render
from django.urls import reverse
from django.views import View

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse


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
        description="Guess locations from your own photos, place names, or Street View - solo or with friends.",
        icon="travel_explore",
        url_name="spotguessr",
    ),
]


class GamesOverviewView(LoginRequiredMixin, View):
    """The games hub: every built-in game.

    GET /games/
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        return render(request, "dashboard/pages/games/index.html", {"page_name": "games", "games": GAMES})
