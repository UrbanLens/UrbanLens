"""Boundary voting - recency-weighted community choice of the official boundary.

When more than one external provider has property geometry for a place
(REData's county parcel vs. Overpass's OpenStreetMap perimeter), users pick
which one is most accurate. The winner becomes ``Place.geometry`` - the
official outline every resolution and access path consumes
(``Place.objects.resolve_for_point``, ``wiki_access``,
``BoundaryManager.resolve_for_*``). Materializing the winner onto the place
(:func:`apply_winning_boundary`) rather than consulting the tally per lookup
is deliberate: resolution runs containment in SQL over many places at once
and cannot call a Python weighting function per row.

The vote is per *place*, not per location, which is what makes it a community
decision at all: everyone who pinned one property is voting on the same
outline instead of each quietly correcting their own copy of it.

Only externally-sourced candidate rows are votable. The spec's whole point
is letting users choose between *accurate official* datasets while still
preventing arbitrarily large hand-drawn boundaries from becoming the match
area - so wiki/pin drawings (any ``Boundary.polygon``) are never options,
and only ``BoundaryType.PROPERTY`` candidates count because the vote is over
which parcel outline is right (a building's footprint has no competing
sources to choose between).

Weighting: each vote is worth ``0.5 ** (age_days / HALF_LIFE_DAYS)`` - a
half-life decay, so a fresh vote outweighs a stale one and two votes of the
same age tie exactly. On an exact tie (including zero votes) REData wins,
then the deterministic source-priority order below.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.utils import timezone

from urbanlens.dashboard.models.boundary.model import Boundary, BoundarySource, BoundaryType
from urbanlens.dashboard.models.boundary_vote.model import BoundaryVote

if TYPE_CHECKING:
    from datetime import datetime

    from urbanlens.dashboard.models.place.model import Place
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

#: Days for a vote's weight to halve. Six months: long enough that a summer
#: of votes still counts in winter, short enough that last year's consensus
#: yields to this month's corrections.
HALF_LIFE_DAYS = 180

#: A leader needs at least this multiple of the runner-up's weight to count
#: as consensus (used to stop auto-prompting for more votes).
CONSENSUS_RATIO = 1.5

#: Deterministic tie-break order: REData is survey-grade county GIS geometry,
#: Overpass is community-tagged - prefer authoritative data on exact ties,
#: exactly as the chain in ``services.locations.boundaries`` orders providers.
_SOURCE_PRIORITY = {
    BoundarySource.REDATA.value: 0,
    BoundarySource.OVERPASS.value: 1,
}


class BoundaryVoteError(Exception):
    """A boundary vote could not be cast (bad candidate, wrong place...).

    ``safe_message`` is safe to surface directly to the caller.
    """

    def __init__(self, message: str) -> None:
        self.safe_message = message
        super().__init__(message)


def vote_weight(voted_at: datetime, now: datetime | None = None) -> float:
    """The recency weight of a vote cast (or last changed) at ``voted_at``.

    Half-life decay: a vote loses half its weight every
    :data:`HALF_LIFE_DAYS`. Votes of identical age get identical weight, and
    a hypothetical future timestamp is clamped to "just now" rather than
    given a super-unit weight.

    Args:
        voted_at: When the vote was cast or last changed.
        now: Evaluation time; defaults to ``timezone.now()``.

    Returns:
        A weight in (0, 1].
    """
    now = now or timezone.now()
    age_days = max((now - voted_at).total_seconds(), 0.0) / 86_400
    return 0.5 ** (age_days / HALF_LIFE_DAYS)


def _priority(boundary: Boundary) -> tuple[int, str, int]:
    """Deterministic tie-break key for a candidate (lower sorts first)."""
    return (_SOURCE_PRIORITY.get(boundary.source, len(_SOURCE_PRIORITY)), boundary.source, boundary.pk)


def boundary_options(place: Place | None) -> list[Boundary]:
    """The votable candidate boundaries for a place, in priority order.

    Only externally-sourced property candidates with actual geometry qualify
    (see the module docstring for why user-drawn rows and building rows are
    excluded).

    Args:
        place: The place whose official boundary may be voted on; None (a
            coordinate no provider knows) has nothing to vote on.

    Returns:
        Candidate Boundary rows, REData first; empty when the provider chain
        hasn't produced per-source candidates for this place.
    """
    if place is None:
        return []
    candidates = Boundary.objects.source_candidates_for_place(place).of_type(BoundaryType.PROPERTY).exclude(generated_polygon__isnull=True)
    return sorted(candidates, key=_priority)


def _weights(place: Place | None, options: list[Boundary], now: datetime | None = None) -> dict[int, float]:
    """Summed recency weight per candidate boundary id (0.0 when unvoted)."""
    now = now or timezone.now()
    weights = dict.fromkeys((option.pk for option in options), 0.0)
    for boundary_id, voted_at in BoundaryVote.objects.for_place(place).filter(boundary_id__in=weights).values_list("boundary_id", "updated"):
        weights[boundary_id] += vote_weight(voted_at, now)
    return weights


def winning_boundary(place: Place | None) -> Boundary | None:
    """The candidate boundary the community's weighted votes select.

    With no votes at all (or an exact weight tie) the REData candidate wins,
    then the next source in priority order - matching the provider chain's
    own REData-first default.

    Args:
        place: The place to resolve the winner for.

    Returns:
        The winning candidate row, or None when the place has no
        candidates at all.
    """
    options = boundary_options(place)
    if not options:
        return None
    weights = _weights(place, options)
    return min(options, key=lambda option: (-weights[option.pk], _priority(option)))


def has_consensus(place: Place | None) -> bool:
    """Whether the community has settled on a boundary for this place.

    Consensus requires at least one vote, and either only one candidate
    having any votes at all or the leader's summed weight being at least
    :data:`CONSENSUS_RATIO` times the runner-up's. Used to stop auto-opening
    the vote dialog - voting stays possible via the manual button.

    Args:
        place: The place to check.

    Returns:
        True once the vote is effectively decided.
    """
    options = boundary_options(place)
    if len(options) < 2:
        # Nothing to decide between - but also nothing contested, so no
        # dialog should be nagging anyone. Report "no consensus" (there is
        # no vote), and let callers gate on option count instead.
        return False
    weights = sorted(_weights(place, options).values(), reverse=True)
    leader, runner_up = weights[0], weights[1]
    if leader <= 0.0:
        return False
    if runner_up <= 0.0:
        return True
    return leader >= CONSENSUS_RATIO * runner_up


def apply_winning_boundary(place: Place | None) -> Boundary | None:
    """Materialize the vote winner onto ``Place.geometry``.

    No-op when nobody has voted: the place then keeps whatever the provider
    chain gave it, which is already REData-first - the spec's zero-vote
    default. With votes, the winner's polygon overwrites the place's geometry
    so every resolution and access path respects the community's choice
    without consulting the tally per lookup.

    Args:
        place: The place whose official boundary should be synced.

    Returns:
        The winning candidate that was applied, or None when nothing changed
        hands (no votes, no candidates, or no geometry).
    """
    from urbanlens.dashboard.models.place.model import Place as PlaceModel
    from urbanlens.dashboard.services.places import resolution

    if place is None or not BoundaryVote.objects.for_place(place).exists():
        return None
    winner = winning_boundary(place)
    if winner is None or winner.generated_polygon is None:
        return None
    if place.geometry is not None and place.geometry.wkb == winner.generated_polygon.wkb:
        return winner
    PlaceModel.objects.filter(pk=place.pk).update(geometry=winner.generated_polygon, updated=timezone.now())
    place.geometry = winner.generated_polygon
    resolution.refresh_area(place)
    # The winning outline may cover ground the losing one didn't (or stop
    # covering ground it did), so who stands on this place can change.
    resolution.resolve_locations_in(winner.generated_polygon)
    logger.info("Boundary vote applied for place %s: %s is now the official boundary", place.pk, winner.source)
    return winner


def cast_boundary_vote(place: Place | None, profile: Profile, boundary_id: int) -> BoundaryVote:
    """Cast or change ``profile``'s vote for one of ``place``'s candidates.

    One row per (place, profile): re-voting updates the row's choice and
    its ``updated`` timestamp, refreshing its recency weight - even when the
    choice is unchanged (re-affirming counts). The canonical boundary is
    re-synced immediately so matching reflects the new tally.

    Args:
        place: The place being voted on.
        profile: The voter.
        boundary_id: PK of the chosen candidate boundary.

    Returns:
        The created or updated vote row.

    Raises:
        BoundaryVoteError: The boundary isn't one of this place's votable
            candidates.
    """
    options = {option.pk: option for option in boundary_options(place)}
    choice = options.get(boundary_id)
    if choice is None:
        raise BoundaryVoteError("That boundary is not a votable option for this place.")
    vote, _created = BoundaryVote.objects.update_or_create(
        place=place,
        profile=profile,
        defaults={"boundary": choice},
    )
    apply_winning_boundary(place)
    return vote


def boundary_vote_context(place: Place | None, profile: Profile | None, *, conceal: bool = False) -> dict | None:
    """Template context for the wiki page's boundary-vote dialog and button.

    Args:
        place: The wiki's place.
        profile: The viewing profile (for their current choice).
        conceal: Whether this viewer sees the concealed form of the wiki.

    Returns:
        None when fewer than two candidates exist (nothing to vote on - no
        button, no dialog). Otherwise a dict with ``options`` (id, source,
        label, GeoJSON polygon, whether it's the viewer's current choice),
        ``my_vote_id``, ``has_votes``, ``has_consensus``, and ``auto_open``
        (True only when nobody *else* has voted yet, per the spec - once other
        people's votes exist the dialog stays behind the manual button).
    """
    from urbanlens.dashboard.services.geo.geo import geometry_to_geojson

    options = boundary_options(place)
    if len(options) < 2:
        return None
    my_vote = BoundaryVote.objects.my_vote(place, profile)
    my_vote_id = my_vote.boundary_id if my_vote else None

    # Other people's votes only, and this is deliberately not "all votes".
    #
    # `auto_open` is an *inverted* tell: a place nobody has voted on opens this
    # dialog on arrival, so a concealed viewer who does not get it has been
    # shown that other people have been here - the concealment announcing
    # itself by staying quiet. Concealing the votes therefore has to conceal
    # them from this predicate too, or the dialog stops opening for exactly the
    # viewer it is hiding them from.
    #
    # Excluding the viewer's own vote also fixes a contradiction that predates
    # concealment: once you had voted, `has_votes` was true because of your own
    # row, so the dialog stopped auto-opening for you on every other place too.
    others = BoundaryVote.objects.for_place(place)
    if profile is not None:
        others = others.exclude(profile=profile)
    if conceal:
        others = others.none()
    has_votes = others.exists()
    return {
        "options": [
            {
                "id": option.pk,
                "source": option.source,
                "label": BoundarySource(option.source).label if option.source in BoundarySource.values else option.source,
                "polygon": geometry_to_geojson(option.generated_polygon),
                "is_my_choice": option.pk == my_vote_id,
            }
            for option in options
        ],
        "my_vote_id": my_vote_id,
        "has_votes": has_votes,
        "has_consensus": has_consensus(place),
        "auto_open": not has_votes,
    }
