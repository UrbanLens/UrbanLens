"""Applying a completed round's results to Glicko-2 ratings.

See ``docs/designs/spotguessr.md`` ("Glicko-2 ratings: player skill vs.
location difficulty") for why a round is treated as one rating period for
both the players and the location.
"""

from __future__ import annotations

from django.utils import timezone

from urbanlens.dashboard.models.spotguessr.model import GameRound, Guess, LocationModeRating, PlayerModeRating
from urbanlens.dashboard.services.spotguessr import glicko2
from urbanlens.dashboard.services.spotguessr.scoring import MAX_ROUND_POINTS


def apply_round_ratings(round_: GameRound, guesses: list[Guess]) -> None:
    """Update every participant's PlayerModeRating and the round's LocationModeRating.

    Must be called exactly once per round, after every participant has
    guessed - calling it twice would double-count the round as two rating
    periods. ``services.spotguessr.session.submit_guess`` is the only
    caller and enforces this.
    """
    if not guesses:
        return

    now = timezone.now()
    mode = round_.session.mode
    location_rating = LocationModeRating.objects.get_or_create_for(round_.location, mode)
    # Both sides of this round's update must see the location's rating
    # *before* any of this round's guesses touch it - captured once, up front.
    location_before = glicko2.Rating(mu=location_rating.mu, phi=location_rating.phi, sigma=location_rating.sigma)
    location_opponents = []

    for guess in guesses:
        fraction = max(0.0, min(1.0, guess.points / MAX_ROUND_POINTS))
        player_rating = PlayerModeRating.objects.get_or_create_for(guess.profile, mode)
        player_before = glicko2.Rating(mu=player_rating.mu, phi=player_rating.phi, sigma=player_rating.sigma)

        updated_player = glicko2.rate(player_before, [glicko2.Opponent(mu=location_before.mu, phi=location_before.phi, score=fraction)])
        player_rating.mu, player_rating.phi, player_rating.sigma = updated_player.mu, updated_player.phi, updated_player.sigma
        player_rating.games_played += 1
        player_rating.last_played_at = now
        player_rating.save(update_fields=["mu", "phi", "sigma", "games_played", "last_played_at", "updated"])

        # The location's opponent-facing rating is the player's rating
        # *before* this round, matching Glicko-2's requirement that both
        # sides of a period use each other's start-of-period ratings.
        location_opponents.append(glicko2.Opponent(mu=player_before.mu, phi=player_before.phi, score=1.0 - fraction))

    updated_location = glicko2.rate(location_before, location_opponents)
    location_rating.mu, location_rating.phi, location_rating.sigma = updated_location.mu, updated_location.phi, updated_location.sigma
    location_rating.games_played += 1
    location_rating.last_used_at = now
    location_rating.save(update_fields=["mu", "phi", "sigma", "games_played", "last_used_at", "updated"])
