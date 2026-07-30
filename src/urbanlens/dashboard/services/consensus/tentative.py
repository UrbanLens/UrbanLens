"""Cross-session tentative-answer accumulation for Consensus.

When a competitive round's disagreement vote fails to reach consensus,
every distinct submitted value is saved here instead of touching the wiki -
a later session proposing the same (or, for coordinates, a nearby) value
bumps ``support_count`` rather than creating a duplicate row, so consensus
can build up over time across separate play sessions (see
``services.consensus.session``'s ``TENTATIVE`` resolution branch).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.utils import timezone

from urbanlens.dashboard.models.consensus.model import ConsensusFieldKind, ConsensusTentativeAnswer

if TYPE_CHECKING:
    from urbanlens.dashboard.models.consensus.model import ConsensusAnswer, ConsensusRound
    from urbanlens.dashboard.models.wiki.model import Wiki

#: How close two coordinate proposals must be to count as the same
#: tentative answer - mirrors ``services.consensus.fields.AGREEMENT_DISTANCE_METERS``.
COORDINATE_MERGE_DISTANCE_METERS = 15.0


def record_tentative_answers(round_: ConsensusRound, distinct_answers: list[ConsensusAnswer]) -> list[ConsensusTentativeAnswer]:
    """Upsert one ``ConsensusTentativeAnswer`` per distinct submitted value in ``distinct_answers``."""
    if round_.field_kind == ConsensusFieldKind.PHOTO_COORDINATES:
        return [_record_coordinate(round_, answer) for answer in distinct_answers]
    return [_record_text(round_, answer) for answer in distinct_answers]


def _record_text(round_: ConsensusRound, answer: ConsensusAnswer) -> ConsensusTentativeAnswer:
    from urbanlens.dashboard.services.locations.naming import normalize_name_for_comparison

    wiki = round_.wiki
    normalized = normalize_name_for_comparison(answer.text_value)
    existing = ConsensusTentativeAnswer.objects.filter(wiki=wiki, field_kind=round_.field_kind, normalized_text=normalized).first()
    if existing is not None:
        existing.support_count += 1
        existing.last_suggested_at = timezone.now()
        existing.save(update_fields=["support_count", "last_suggested_at", "updated"])
        return existing
    return ConsensusTentativeAnswer.objects.create(
        wiki=wiki,
        field_kind=round_.field_kind,
        text_value=answer.text_value,
        normalized_text=normalized,
        source_round=round_,
    )


def _record_coordinate(round_: ConsensusRound, answer: ConsensusAnswer) -> ConsensusTentativeAnswer:
    from urbanlens.dashboard.services.consensus.fields import haversine_distance_meters

    # Only ever called for a PHOTO_COORDINATES round's real (non-skip)
    # answers - services.consensus.session guarantees guess_point is set
    # before this runs; the field itself is nullable only because the same
    # column is shared with text-kind answers.
    if answer.guess_point is None:
        raise ValueError(f"ConsensusAnswer {answer.pk} has no guess_point but was routed through PHOTO_COORDINATES tentative-answer recording.")
    wiki = round_.wiki
    for existing in ConsensusTentativeAnswer.objects.filter(wiki=wiki, field_kind=ConsensusFieldKind.PHOTO_COORDINATES, target_image=round_.target_image):
        if existing.guess_point is not None and haversine_distance_meters(existing.guess_point, answer.guess_point) <= COORDINATE_MERGE_DISTANCE_METERS:
            existing.support_count += 1
            existing.last_suggested_at = timezone.now()
            existing.save(update_fields=["support_count", "last_suggested_at", "updated"])
            return existing
    return ConsensusTentativeAnswer.objects.create(
        wiki=wiki,
        field_kind=ConsensusFieldKind.PHOTO_COORDINATES,
        target_image=round_.target_image,
        guess_point=answer.guess_point,
        source_round=round_,
    )
