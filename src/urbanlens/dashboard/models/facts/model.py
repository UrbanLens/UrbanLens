"""Facts - a confidence-tracked piece of data about a Location, Wiki, or Image.

A ``Fact`` is the *resolved* current state (value, confidence, status) for one
``(subject, key)`` pair - e.g. "this wiki's ``indoor_outdoor`` is BOTH, at
0.82 confidence". It is never written to directly; it is always recomputed
from its ``FactEvidence`` trail (see ``services.facts.confidence``) by
whichever source contributed a new observation (SpotGuessr guesses, Consensus
answers, manual wiki edits, future AI extraction - see
``services.facts.evidence``).

Subject attachment follows this codebase's established pattern (no
``GenericForeignKey`` is used anywhere - see ``ConsensusRound.wiki`` +
``.target_image``, ``models/consensus/model.py``): one nullable FK per
possible subject type, with exactly one populated. A new subject type in the
future is one more additive nullable FK, never a schema migration of existing
rows.

``key`` is a free string validated against ``services.facts.registry`` rather
than a fixed Django enum, so a new fact key never requires a migration.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib.gis.db.models import PointField
from django.db.models import (
    CASCADE,
    SET_NULL,
    BooleanField,
    CharField,
    DateField,
    DateTimeField,
    FloatField,
    ForeignKey,
    Index,
    JSONField,
    PositiveIntegerField,
    Q,
)
from django.db.models.constraints import UniqueConstraint

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.facts.queryset import FactEvidenceManager, FactManager


class FactDataType(abstract.TextChoices):
    """How a Fact/FactEvidence's value is stored and compared.

    Snapshotted onto each row at creation time from ``services.facts.
    registry.FactKeyDefinition.data_type`` - old rows stay interpretable even
    if a key's registered type is ever changed.
    """

    TEXT = "text", "Text"
    NUMBER = "number", "Number"
    DATE = "date", "Date"
    POINT = "point", "Point"
    BOOL = "bool", "Boolean"
    CHOICE = "choice", "Choice"


class FactSubjectType(abstract.TextChoices):
    """Which of ``Fact``'s nullable subject FKs is populated - denormalized for filtering/display."""

    LOCATION = "location", "Location"
    WIKI = "wiki", "Wiki"
    IMAGE = "image", "Image"


class FactStatus(abstract.TextChoices):
    """Lifecycle of a Fact's confidence, recomputed by ``services.facts.confidence.recompute``."""

    UNCONFIRMED = "unconfirmed", "Unconfirmed"
    TENTATIVE = "tentative", "Tentative"
    CONFIRMED = "confirmed", "Confirmed"
    CONTESTED = "contested", "Contested"


class FactSourceKind(abstract.TextChoices):
    """Broad category of where one piece of ``FactEvidence`` came from.

    ``source_name`` (a free slug on ``FactEvidence``) narrows within a kind -
    e.g. ``PLAYER_ATTRIBUTED``/``"consensus"`` vs. a future
    ``PLAYER_ATTRIBUTED``/other-game source.
    """

    PLAYER_ANONYMOUS = "player_anonymous", "Anonymous player"
    PLAYER_ATTRIBUTED = "player_attributed", "Attributed player"
    WIKI_EDIT = "wiki_edit", "Manual wiki edit"
    AI_AGENT = "ai_agent", "AI agent"
    EXTERNAL_SOURCE = "external_source", "External data source"
    ADMIN = "admin", "Admin/staff"


#: Which typed ``value_*`` column stores a value of each ``FactDataType``.
_VALUE_FIELD_BY_DATA_TYPE: dict[str, str] = {
    FactDataType.TEXT: "value_text",
    FactDataType.CHOICE: "value_text",
    FactDataType.NUMBER: "value_number",
    FactDataType.DATE: "value_date",
    FactDataType.POINT: "value_point",
    FactDataType.BOOL: "value_bool",
}


def value_field_name(data_type: str) -> str:
    """The ``value_*`` column that stores a value of ``data_type``.

    Args:
        data_type: A ``FactDataType`` value.

    Returns:
        The matching column name on ``TypedValueModel``.

    Raises:
        ValueError: If ``data_type`` isn't a recognized ``FactDataType``.
    """
    try:
        return _VALUE_FIELD_BY_DATA_TYPE[data_type]
    except KeyError:
        raise ValueError(f"Unknown fact data type: {data_type!r}") from None


class TypedValueModel(abstract.DashboardModel):
    """Abstract base carrying one typed-value column per ``FactDataType``, plus get/set helpers.

    Shared by ``Fact`` (the resolved current value) and ``FactEvidence`` (one
    observed value) so the five value columns and their dispatch logic are
    declared once.
    """

    data_type = CharField(max_length=6, choices=FactDataType.choices)
    value_text = CharField(max_length=500, null=True, blank=True)
    value_number = FloatField(null=True, blank=True)
    value_date = DateField(null=True, blank=True)
    value_point = PointField(geography=True, srid=4326, null=True, blank=True)
    value_bool = BooleanField(null=True)

    class Meta(abstract.DashboardModel.Meta):
        abstract = True

    def get_value(self) -> Any:
        """The value in whichever ``value_*`` column matches ``self.data_type``."""
        return getattr(self, value_field_name(self.data_type))

    def set_value(self, value: Any) -> None:
        """Write ``value`` into whichever ``value_*`` column matches ``self.data_type``."""
        setattr(self, value_field_name(self.data_type), value)


class Fact(TypedValueModel):
    """The resolved current value/confidence/status for one ``(subject, key)`` pair.

    Never written to directly - always recomputed from ``self.evidence`` by
    ``services.facts.confidence.recompute``, queued async
    (``tasks.recompute_fact_confidence``) after every new ``FactEvidence`` row.

    Attributes:
        key: Which piece of data this is, e.g. ``"wiki_indoor_outdoor"`` -
            validated against ``services.facts.registry``, not a fixed enum.
        subject_type: Denormalized from whichever of ``location``/``wiki``/
            ``image`` is set - lets admin/queries filter without inspecting
            three columns.
        confidence: In ``[0, 1]`` - see ``services.facts.confidence`` for how
            it's derived from evidence.
        status: See ``FactStatus``.
        evidence_count: Cached count of non-superseded evidence rows.
        last_evidence_at: When the most recent evidence row was created.
        last_recomputed_at: When ``services.facts.confidence.recompute`` last ran.
    """

    key = CharField(max_length=64, db_index=True)
    subject_type = CharField(max_length=8, choices=FactSubjectType.choices, editable=False)

    confidence = FloatField(default=0.0)
    status = CharField(max_length=11, choices=FactStatus.choices, default=FactStatus.UNCONFIRMED)
    evidence_count = PositiveIntegerField(default=0)
    last_evidence_at = DateTimeField(null=True, blank=True)
    last_recomputed_at = DateTimeField(null=True, blank=True)

    location = ForeignKey("dashboard.Location", on_delete=CASCADE, null=True, blank=True, related_name="facts")
    wiki = ForeignKey("dashboard.Wiki", on_delete=CASCADE, null=True, blank=True, related_name="facts")
    image = ForeignKey("dashboard.Image", on_delete=CASCADE, null=True, blank=True, related_name="facts")

    if TYPE_CHECKING:
        location_id: int | None
        wiki_id: int | None
        image_id: int | None

    objects = FactManager()

    def save(self, *args, **kwargs) -> None:
        """Derive ``subject_type`` and enforce "exactly one subject FK set" before saving.

        Backstopped, as an unbypassable floor, by a DB ``CHECK`` constraint
        added in migration ``0020_facts`` - mirrors ``Location``'s own
        immutable-fields enforcement (both in ``save()`` and via a DB trigger).

        Raises:
            ValueError: If zero or more than one of ``location``/``wiki``/
                ``image`` is set.
        """
        subject_count = sum(subject_id is not None for subject_id in (self.location_id, self.wiki_id, self.image_id))
        if subject_count != 1:
            raise ValueError("Fact must have exactly one of location, wiki, or image set.")

        if self.location_id is not None:
            self.subject_type = FactSubjectType.LOCATION
        elif self.wiki_id is not None:
            self.subject_type = FactSubjectType.WIKI
        else:
            self.subject_type = FactSubjectType.IMAGE

        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"Fact(subject_type={self.subject_type}, key={self.key}, status={self.status}, confidence={self.confidence:.2f})"

    class Meta(TypedValueModel.Meta):
        db_table = "dashboard_facts"
        indexes = [
            Index(fields=["status"], name="idxdb_fact_status"),
        ]
        constraints = [
            UniqueConstraint(fields=["location", "key"], condition=Q(location__isnull=False), name="db_fact_unique_location_key"),
            UniqueConstraint(fields=["wiki", "key"], condition=Q(wiki__isnull=False), name="db_fact_unique_wiki_key"),
            UniqueConstraint(fields=["image", "key"], condition=Q(image__isnull=False), name="db_fact_unique_image_key"),
        ]


class FactEvidence(TypedValueModel):
    """One append-only observation contributing toward a Fact's confidence.

    Never mutated or deleted once created - a bad observation is
    soft-invalidated via ``superseded`` instead (mirrors ``WikiEdit.reverted``),
    so the evidence trail always reflects exactly what was submitted and when.

    Attributes:
        source_kind: Broad category of where this observation came from.
        source_name: Free slug narrowing within ``source_kind`` (e.g.
            ``"spotguessr_photo_guess"``, ``"consensus"``, ``"manual_edit"``) -
            mirrors ``NameCandidate.source``.
        submitter: Who submitted this, if attributable - null for
            anonymous-by-design sources (mirrors ``PhotoCoordinateGuess``).
        submitter_trust_snapshot: ``ConsensusProfile.trust_score`` at
            submission time, for sources with a per-submitter trust concept -
            null otherwise.
        source_reliability: Static weight for this ``(source_kind,
            source_name)`` pair - see ``services.facts.evidence.
            SOURCE_RELIABILITY``.
        consensus_round: The round this observation came from, if any.
        context: Free provenance metadata (e.g. an upstream row id, for
            debugging) - never used for uniqueness/idempotency, which is a
            call-site responsibility.
        superseded: True once this observation has been soft-invalidated and
            should be excluded from confidence recomputation.
    """

    source_kind = CharField(max_length=17, choices=FactSourceKind.choices)
    source_name = CharField(max_length=50, blank=True, default="")

    submitter = ForeignKey("dashboard.Profile", on_delete=SET_NULL, null=True, blank=True, related_name="fact_evidence")
    submitter_trust_snapshot = FloatField(null=True, blank=True)
    source_reliability = FloatField(default=1.0)

    fact = ForeignKey("dashboard.Fact", on_delete=CASCADE, related_name="evidence")
    consensus_round = ForeignKey("dashboard.ConsensusRound", on_delete=SET_NULL, null=True, blank=True, related_name="fact_evidence")
    context = JSONField(default=dict, blank=True)
    superseded = BooleanField(default=False)

    if TYPE_CHECKING:
        fact_id: int
        submitter_id: int | None
        consensus_round_id: int | None

    objects = FactEvidenceManager()

    def __str__(self) -> str:
        return f"FactEvidence(fact={self.fact_id}, source_kind={self.source_kind}, source_name={self.source_name!r})"

    class Meta(TypedValueModel.Meta):
        db_table = "dashboard_fact_evidence"
        indexes = [
            Index(fields=["fact", "created"], name="idxdb_factev_fact_created"),
            Index(fields=["source_kind"], name="idxdb_factev_source_kind"),
        ]
