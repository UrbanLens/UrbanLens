"""External-API routes for the gamified community-data surfaces.

Owns SpotGuessr, Trivia and Consensus: starting a session, fetching the next
round, submitting a guess or an answer, and reading scores, points, levels and
leaderboards. These games exist to get real data into wikis, so their write
paths have side effects on community content - which is exactly why they are
routed as their own domain rather than being folded into ``urls_wiki_community``:
a client's game scope should not imply the ability to edit a wiki directly.

Sessions are stateful and time-bounded (a stalled session is abandoned rather
than left open forever), so endpoints here are session-scoped -
``games/<...>/sessions/<...>/`` - and never trust a client-supplied score.

Wiring: ``urls.py`` concatenates the ``urlpatterns`` below into the flat
``external_api:`` namespace and re-sorts the combined list with
:func:`~urbanlens.dashboard.external_api.urls.order_by_specificity`, so
declaration order inside this module only breaks ties between routes of
identical shape. Use ``path()`` (``re_path()`` cannot be ordered and is
rejected at import time) and keep every ``name=`` unique across the whole
external API.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.urls import path

from urbanlens.dashboard.external_api import views_games

if TYPE_CHECKING:
    from django.urls.resolvers import URLPattern

#: Routes contributed by this domain. Appended to the flat ``external_api:``
#: namespace by ``urls.py`` - see this module's docstring before adding to it.
#:
#: SpotGuessr only, and solo-play only: the lobby/invite/join/begin/end/chat
#: half of the game is deliberately not routed here (see ``views_games`` for
#: why mirroring it would strand a client mid-round). Session ids are ``int``
#: because ``GameSession`` has no uuid - the specificity sort in ``urls.py``
#: puts ``int`` converters ahead of ``str``/``slug`` ones, so these cannot be
#: shadowed by another domain's generic route.
urlpatterns: list[URLPattern] = [
    path("games/spotguessr/", views_games.SpotGuessrOverviewView.as_view(), name="games.spotguessr"),
    path("games/spotguessr/preferences/", views_games.SpotGuessrPreferencesView.as_view(), name="games.spotguessr.preferences"),
    path("games/spotguessr/eligible-count/", views_games.SpotGuessrEligibleCountView.as_view(), name="games.spotguessr.eligible-count"),
    path("games/spotguessr/eligible-pins/", views_games.SpotGuessrEligiblePinsView.as_view(), name="games.spotguessr.eligible-pins"),
    path("games/spotguessr/sessions/", views_games.SpotGuessrSessionsView.as_view(), name="games.spotguessr.sessions"),
    path("games/spotguessr/sessions/<int:session_id>/", views_games.SpotGuessrSessionDetailView.as_view(), name="games.spotguessr.sessions.detail"),
    path("games/spotguessr/sessions/<int:session_id>/round/", views_games.SpotGuessrRoundView.as_view(), name="games.spotguessr.sessions.round"),
    path("games/spotguessr/sessions/<int:session_id>/summary/", views_games.SpotGuessrSummaryView.as_view(), name="games.spotguessr.sessions.summary"),
    path("games/spotguessr/sessions/<int:session_id>/rounds/<int:round_id>/guess/", views_games.SpotGuessrGuessView.as_view(), name="games.spotguessr.sessions.rounds.guess"),
    path("games/spotguessr/sessions/<int:session_id>/rounds/<int:round_id>/image/", views_games.SpotGuessrRoundImageView.as_view(), name="games.spotguessr.sessions.rounds.image"),
    path("games/spotguessr/sessions/<int:session_id>/rounds/<int:round_id>/expire/", views_games.SpotGuessrRoundExpireView.as_view(), name="games.spotguessr.sessions.rounds.expire"),
    path("games/spotguessr/sessions/<int:session_id>/rounds/<int:round_id>/feedback/", views_games.SpotGuessrRoundFeedbackView.as_view(), name="games.spotguessr.sessions.rounds.feedback"),
]
