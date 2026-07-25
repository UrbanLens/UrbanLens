"""Applying a completed round's results to Glicko-2 ratings.

Mirrors ``services.spotguessr.ratings.apply_round_ratings`` exactly, reusing
``services.spotguessr.glicko2``'s pure math directly rather than
reimplementing it. A round is one rating period for both the players and the
question, treating "answered correctly" as score 1.0 and "answered
incorrectly" as score 0.0 - a binary outcome, unlike SpotGuessr's continuous
distance-based fraction, but the same player-skill/content-difficulty pairing.
"""

from __future__ import annotations

from django.utils import timezone

from urbanlens.dashboard.models.trivia.model import PlayerTriviaRating, TriviaAnswer, TriviaQuestionRating, TriviaRound
from urbanlens.dashboard.services.spotguessr import glicko2


def apply_round_ratings(round_: TriviaRound, answers: list[TriviaAnswer]) -> None:
    """Update every participant's PlayerTriviaRating and the round's TriviaQuestionRating.

    Must be called exactly once per round, after every participant has
    answered - calling it twice would double-count the round as two rating
    periods. ``services.trivia.session.submit_answer`` is the only caller
    and enforces this.
    """
    if not answers:
        return

    now = timezone.now()
    question_rating = TriviaQuestionRating.objects.get_or_create_for(round_.question)
    # Both sides of this round's update must see the question's rating
    # *before* any of this round's answers touch it - captured once, up front.
    question_before = glicko2.Rating(mu=question_rating.mu, phi=question_rating.phi, sigma=question_rating.sigma)
    question_opponents = []

    for answer in answers:
        score = 1.0 if answer.is_correct else 0.0
        player_rating = PlayerTriviaRating.objects.get_or_create_for(answer.profile)
        player_before = glicko2.Rating(mu=player_rating.mu, phi=player_rating.phi, sigma=player_rating.sigma)

        updated_player = glicko2.rate(player_before, [glicko2.Opponent(mu=question_before.mu, phi=question_before.phi, score=score)])
        player_rating.mu, player_rating.phi, player_rating.sigma = updated_player.mu, updated_player.phi, updated_player.sigma
        player_rating.games_played += 1
        player_rating.last_played_at = now
        player_rating.save(update_fields=["mu", "phi", "sigma", "games_played", "last_played_at", "updated"])

        # The question's opponent-facing rating is the player's rating
        # *before* this round, matching Glicko-2's requirement that both
        # sides of a period use each other's start-of-period ratings.
        question_opponents.append(glicko2.Opponent(mu=player_before.mu, phi=player_before.phi, score=1.0 - score))

    updated_question = glicko2.rate(question_before, question_opponents)
    question_rating.mu, question_rating.phi, question_rating.sigma = updated_question.mu, updated_question.phi, updated_question.sigma
    question_rating.games_played += 1
    question_rating.last_asked_at = now
    question_rating.save(update_fields=["mu", "phi", "sigma", "games_played", "last_asked_at", "updated"])
