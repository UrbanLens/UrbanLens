"""Applying a completed round's results to Glicko-2 ratings.

See ``docs/designs/drafts/spotguessr.md`` ("Glicko-2 ratings: player skill vs.
location difficulty") for why a round is treated as one rating period for
both the players and the location.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from urbanlens.dashboard.models.spotguessr.model import GameRound, Guess, LocationModeRating, PlayerModeRating
from urbanlens.dashboard.services.spotguessr import glicko2
from urbanlens.dashboard.services.spotguessr.scoring import MAX_ROUND_POINTS


@dataclass(frozen=True)
class RatingChange:
    """One profile's display-scale Glicko-2 rating, before and after a single round.

    Surfaced all the way out to the frontend (``serializers.serialize_reveal``/
    ``serialize_round_reveal``) so a round's reveal can show "▲ +14 rating" -
    see the SpotGuessr audit's "the game computes your rating change every
    round and never shows it to you" finding. Both values are display-scale
    (``PlayerModeRating.rating``, centered on 1500), not the raw Glicko-2
    ``mu``/``phi`` this module otherwise operates on.
    """

    rating_before: float
    rating_after: float

    @property
    def delta(self) -> float:
        """Net rating change from this one round - positive is an improvement."""
        return self.rating_after - self.rating_before


@transaction.atomic
def apply_round_ratings(round_: GameRound, guesses: list[Guess]) -> dict[int, RatingChange]:
    """Update every participant's PlayerModeRating and the round's LocationModeRating.

    Must be called exactly once per round, after every participant has
    guessed - calling it twice would double-count the round as two rating
    periods. ``services.spotguessr.session._finish_round`` is the only
    caller and enforces this.

    Returns:
        Each guessing profile's own ``RatingChange`` from this round, keyed
        by profile id - the immediate "how did I do" signal a round's reveal
        can't get from anywhere else, since the before/after ratings only
        ever exist in-memory here.
    """
    if not guesses:
        return {}

    now = timezone.now()
    mode = round_.session.mode
    location_rating = LocationModeRating.objects.get_or_create_for(round_.location, mode)
    # Locked first, before any player row: this is the row two rounds contend for
    # (the same location played in two sessions at once), and taking the shared row
    # first gives every caller one lock order. Without it both rounds compute from
    # the same location_before and the second save discards the first round entirely.
    location_rating.refresh_from_db(from_queryset=LocationModeRating.objects.select_for_update())
    # Both sides of this round's update must see the location's rating
    # *before* any of this round's guesses touch it - captured once, up front.
    location_before = glicko2.Rating(mu=location_rating.mu, phi=location_rating.phi, sigma=location_rating.sigma)
    location_opponents = []
    rating_changes: dict[int, RatingChange] = {}

    for guess in guesses:
        # bonus_points (country/state/city) folds in here - it's the same
        # "know where this is" skill the rating measures, unlike date_points
        # (deliberately excluded - see Guess.bonus_points' docstring).
        fraction = max(0.0, min(1.0, (guess.points + guess.bonus_points) / MAX_ROUND_POINTS))
        player_rating = PlayerModeRating.objects.get_or_create_for(guess.profile, mode)
        player_rating.refresh_from_db(from_queryset=PlayerModeRating.objects.select_for_update())
        player_before = glicko2.Rating(mu=player_rating.mu, phi=player_rating.phi, sigma=player_rating.sigma)
        rating_before_display = player_rating.rating

        updated_player = glicko2.rate(player_before, [glicko2.Opponent(mu=location_before.mu, phi=location_before.phi, score=fraction)])
        player_rating.mu, player_rating.phi, player_rating.sigma = updated_player.mu, updated_player.phi, updated_player.sigma
        player_rating.games_played += 1
        player_rating.last_played_at = now
        player_rating.save(update_fields=["mu", "phi", "sigma", "games_played", "last_played_at", "updated"])
        rating_changes[guess.profile_id] = RatingChange(rating_before=rating_before_display, rating_after=player_rating.rating)

        # The location's opponent-facing rating is the player's rating
        # *before* this round, matching Glicko-2's requirement that both
        # sides of a period use each other's start-of-period ratings.
        location_opponents.append(glicko2.Opponent(mu=player_before.mu, phi=player_before.phi, score=1.0 - fraction))

    updated_location = glicko2.rate(location_before, location_opponents)
    location_rating.mu, location_rating.phi, location_rating.sigma = updated_location.mu, updated_location.phi, updated_location.sigma
    location_rating.games_played += 1
    location_rating.last_used_at = now
    location_rating.save(update_fields=["mu", "phi", "sigma", "games_played", "last_used_at", "updated"])
    return rating_changes
