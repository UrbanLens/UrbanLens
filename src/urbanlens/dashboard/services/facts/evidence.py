"""Writing new FactEvidence rows - the single write path every source funnels through.

Callers never touch ``Fact``/``FactEvidence`` directly: get-or-creating the
resolved ``Fact`` row, snapshotting the key's registered ``data_type``, and
queuing the async confidence recompute are all handled by
:func:`record_evidence`, so every source (SpotGuessr, Consensus, manual wiki
edits, future AI extraction) stays consistent. See
``services.facts.registry`` for the key/data-type registry and
``services.facts.confidence`` for what happens once evidence lands.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from urbanlens.dashboard.models.facts.model import Fact, FactEvidence, FactSourceKind, FactSubjectType
from urbanlens.dashboard.services.facts import registry

if TYPE_CHECKING:
    from django.contrib.gis.geos import Point

    from urbanlens.dashboard.models.consensus.model import ConsensusAnswer, ConsensusRound
    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.models.wiki_edit.model import WikiEdit

logger = logging.getLogger(__name__)

#: Static reliability weight applied when a ``(source_kind, source_name)``
#: pair has no more specific entry in ``SOURCE_RELIABILITY`` below - see
#: ``services.facts.confidence``.
DEFAULT_SOURCE_RELIABILITY = 0.7

#: Per-source reliability weights - deliberately conservative starting
#: points, easy to retune without touching the confidence math itself.
SOURCE_RELIABILITY: dict[tuple[str, str], float] = {
    (FactSourceKind.PLAYER_ANONYMOUS, "spotguessr_photo_guess"): 0.6,
    (FactSourceKind.PLAYER_ATTRIBUTED, "consensus"): 0.8,
    (FactSourceKind.WIKI_EDIT, "manual_edit"): 0.9,
    (FactSourceKind.ADMIN, "admin"): 1.0,
}

#: Maps a Consensus round's field kind to the Fact key it feeds -
#: ``WIKI_ALIAS`` is deliberately absent (excluded from Facts: additive/
#: multi-valued, doesn't fit a single-converging-value Fact key).
CONSENSUS_FIELD_KIND_TO_FACT_KEY: dict[str, str] = {
    "wiki_name": "wiki_name",
    "wiki_description": "wiki_description",
    "wiki_indoor_outdoor": "wiki_indoor_outdoor",
    "wiki_pin_type": "wiki_pin_type",
    "photo_coordinates": "photo_coordinates",
}

#: Maps a ``WikiEdit.changes`` diff key to the Fact key it feeds.
WIKI_EDIT_FIELD_TO_FACT_KEY: dict[str, str] = {
    "name": "wiki_name",
    "description": "wiki_description",
    "indoor_outdoor": "wiki_indoor_outdoor",
    "pin_type": "wiki_pin_type",
}


def _reliability_for(source_kind: str, source_name: str) -> float:
    return SOURCE_RELIABILITY.get((source_kind, source_name), DEFAULT_SOURCE_RELIABILITY)


def _get_or_create_fact(
    *,
    key: str,
    location: Location | None,
    wiki: Wiki | None,
    image: Image | None,
) -> Fact | None:
    definition = registry.get_definition(key)
    if definition is None:
        logger.warning("Ignoring evidence for unregistered fact key %r", key)
        return None

    lookup: dict[str, Any]
    if location is not None:
        subject_type, lookup = FactSubjectType.LOCATION, {"location": location}
    elif wiki is not None:
        subject_type, lookup = FactSubjectType.WIKI, {"wiki": wiki}
    elif image is not None:
        subject_type, lookup = FactSubjectType.IMAGE, {"image": image}
    else:
        raise ValueError("Exactly one of location, wiki, or image must be given.")

    if subject_type not in definition.allowed_subject_types:
        raise ValueError(f"Fact key {key!r} does not allow subject type {subject_type!r}.")

    fact, _created = Fact.objects.get_or_create(key=key, defaults={"data_type": definition.data_type}, **lookup)
    return fact


def record_evidence(
    *,
    key: str,
    value: Any,
    source_kind: str,
    source_name: str = "",
    location: Location | None = None,
    wiki: Wiki | None = None,
    image: Image | None = None,
    submitter: Profile | None = None,
    submitter_trust_snapshot: float | None = None,
    consensus_round: ConsensusRound | None = None,
    context: dict[str, Any] | None = None,
) -> FactEvidence | None:
    """Record one observation toward a Fact, creating the Fact row if this is the first.

    Queues an async confidence recompute (``tasks.recompute_fact_confidence``)
    rather than recomputing inline, per the project's Celery-everything-slow
    standard.

    Args:
        key: A key registered in ``services.facts.registry``.
        value: The observed value, already shaped for the key's data type
            (e.g. a ``Point`` for a POINT key).
        source_kind: A ``FactSourceKind`` value.
        source_name: Free slug narrowing within ``source_kind``.
        location: Subject, when this fact is Location-scoped.
        wiki: Subject, when this fact is Wiki-scoped.
        image: Subject, when this fact is Image-scoped.
        submitter: Who submitted this, if attributable.
        submitter_trust_snapshot: The submitter's trust score at submission
            time, for sources with a per-submitter trust concept.
        consensus_round: The round this observation came from, if any.
        context: Free provenance metadata.

    Returns:
        The created evidence row, or None if ``key`` isn't registered, or the
        subject doesn't allow it.
    """
    fact = _get_or_create_fact(key=key, location=location, wiki=wiki, image=image)
    if fact is None:
        return None

    evidence = FactEvidence(
        fact=fact,
        data_type=fact.data_type,
        source_kind=source_kind,
        source_name=source_name,
        submitter=submitter,
        submitter_trust_snapshot=submitter_trust_snapshot,
        source_reliability=_reliability_for(source_kind, source_name),
        consensus_round=consensus_round,
        context=context or {},
    )
    evidence.set_value(value)
    evidence.save()

    from urbanlens.dashboard import tasks

    tasks.recompute_fact_confidence.delay(fact.pk)
    return evidence


def record_photo_coordinate_evidence(image_id: int, guess_point: Point) -> FactEvidence | None:
    """Log one anonymous SpotGuessr guess toward an image's ``photo_coordinates`` fact.

    Mirrors ``PhotoCoordinateGuess``'s own anonymized-by-design shape - no
    submitter, no round reference, just the point. Called from
    ``services.spotguessr.photo_coordinates.record_guess`` alongside (not
    instead of) the existing ``PhotoCoordinateGuess``/``estimated_latitude``
    machinery, which stays authoritative for existing consumers.

    Args:
        image_id: pk of the ``Image`` this guess is about.
        guess_point: Where the player clicked or picked from pin search.

    Returns:
        The created evidence row, or None if the image no longer exists.
    """
    from urbanlens.dashboard.models.images.model import Image

    try:
        image = Image.objects.get(pk=image_id)
    except Image.DoesNotExist:
        return None

    return record_evidence(
        key="photo_coordinates",
        value=guess_point,
        source_kind=FactSourceKind.PLAYER_ANONYMOUS,
        source_name="spotguessr_photo_guess",
        image=image,
    )


def record_consensus_answer_evidence(round_: ConsensusRound, answer: ConsensusAnswer) -> FactEvidence | None:
    """Log one Consensus player's answer as evidence, trust-weighted by their ``ConsensusProfile``.

    The caller (``services.consensus.session._finish_round``) skips this for
    trust-check-round answers (they measure player accuracy against an
    already-known value, not new signal about the fact) and for
    ``WIKI_ALIAS`` rounds (excluded from Facts entirely - see
    ``CONSENSUS_FIELD_KIND_TO_FACT_KEY``).

    Args:
        round_: The round ``answer`` was submitted for.
        answer: A real (non-skip) answer.

    Returns:
        The created evidence row, or None if this round's field kind isn't
        Facts-tracked, or the answer has no usable value.
    """
    from urbanlens.dashboard.models.consensus.model import ConsensusProfile

    fact_key = CONSENSUS_FIELD_KIND_TO_FACT_KEY.get(round_.field_kind)
    if fact_key is None:
        return None

    value = answer.guess_point if answer.guess_point is not None else answer.text_value
    if value is None:
        return None

    trust_score = ConsensusProfile.objects.get_or_create_for(answer.profile).trust_score

    subject: dict[str, Any]
    if fact_key == "photo_coordinates":
        if round_.target_image_id is None:
            return None
        subject = {"image": round_.target_image}
    else:
        subject = {"wiki": round_.wiki}

    return record_evidence(
        key=fact_key,
        value=value,
        source_kind=FactSourceKind.PLAYER_ATTRIBUTED,
        source_name="consensus",
        submitter=answer.profile,
        submitter_trust_snapshot=trust_score,
        consensus_round=round_,
        **subject,
    )


def record_wiki_edit_evidence(edit: WikiEdit) -> list[FactEvidence]:
    """Log every Facts-mapped field change in a manual ``WikiEdit`` as evidence.

    Only for edits made outside Consensus - the caller
    (``models.wiki_edit.signals``) already guards on
    ``edit.consensus_round_id is None``, since Consensus-sourced edits are
    logged directly from the submitted answers by
    :func:`record_consensus_answer_evidence` at round-resolution time;
    logging both would double-count the same observation.

    Args:
        edit: The manual edit that was just saved.

    Returns:
        The created evidence rows (zero or more - only mapped fields count).
    """
    created = []
    for field_name, change in (edit.changes or {}).items():
        fact_key = WIKI_EDIT_FIELD_TO_FACT_KEY.get(field_name)
        if fact_key is None:
            continue
        new_value = change.get("to") if isinstance(change, dict) else None
        if new_value is None:
            continue
        evidence = record_evidence(
            key=fact_key,
            value=new_value,
            source_kind=FactSourceKind.WIKI_EDIT,
            source_name="manual_edit",
            wiki=edit.wiki,
            submitter=edit.editor,
        )
        if evidence is not None:
            created.append(evidence)
    return created


def record_ai_evidence(
    *,
    key: str,
    value: Any,
    source_name: str,
    location: Location | None = None,
    wiki: Wiki | None = None,
    image: Image | None = None,
    context: dict[str, Any] | None = None,
) -> FactEvidence | None:
    """Log one AI-agent-sourced observation.

    A ready seam, not called anywhere yet - real wiring belongs to whichever
    future ticket adds structured extraction (e.g. a Wikidata/OpenPlaques
    source, ``docs/ROADMAP.md``), which needs to parse discrete claims out of
    otherwise-freeform model output before it has anything to pass here.

    Args:
        key: A key registered in ``services.facts.registry``.
        value: The extracted value, already shaped for the key's data type.
        source_name: Free slug identifying the specific AI source/model.
        location: Subject, when this fact is Location-scoped.
        wiki: Subject, when this fact is Wiki-scoped.
        image: Subject, when this fact is Image-scoped.
        context: Free provenance metadata (e.g. the source document/URL).

    Returns:
        The created evidence row, or None if ``key`` isn't registered.
    """
    return record_evidence(
        key=key,
        value=value,
        source_kind=FactSourceKind.AI_AGENT,
        source_name=source_name,
        location=location,
        wiki=wiki,
        image=image,
        context=context,
    )
