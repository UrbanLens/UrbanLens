"""Which providers' property outlines may be voted on, in what order, and how they are described."""

from __future__ import annotations

from urbanlens.dashboard.plugins.points import BOUNDARY_PROVIDERS


def vote_sources() -> dict[str, str]:
    """
    Provider ``service_key`` to the ``Boundary.source`` value its property geometry is stored under, in chain order.

    Building-footprint providers are absent on purpose: they never produce property boundaries, and the vote is over
    which property boundary should officially describe a place.
    """
    return {provider.service_key: provider.vote_source for provider in BOUNDARY_PROVIDERS if provider.service_key and provider.vote_source}


def vote_labels() -> dict[str, str]:
    """``Boundary.source`` value to how a candidate from it is described to people."""
    return {provider.vote_source: provider.vote_label for provider in BOUNDARY_PROVIDERS if provider.vote_source and provider.vote_label}


def vote_rank(source: str) -> int:
    """
    A source's place in the tie-break order: the chain order of the providers that produce it, authoritative data first.

    Args:
        source: A ``Boundary.source`` value.

    Returns:
        Its position; past every known source for one no provider produces any more.
    """
    ordered = list(dict.fromkeys(vote_sources().values()))
    return ordered.index(source) if source in ordered else len(ordered)
