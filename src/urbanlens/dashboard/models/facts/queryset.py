"""QuerySets/Managers for Fact and FactEvidence.

Confidence math lives in ``services.facts.confidence``; the write path in
``services.facts.evidence``; read-side consumption queries (AI agents,
Consensus recheck-round selection) in ``services.facts.consumption``. These
classes only scope and fetch rows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.facts.model import Fact
    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.wiki.model import Wiki


class FactQuerySet(abstract.DashboardQuerySet):
    """QuerySet for Fact."""

    def for_wiki(self, wiki: Wiki) -> Self:
        """Every fact attached to ``wiki``."""
        return self.filter(wiki=wiki)

    def for_location(self, location: Location) -> Self:
        """Every fact attached to ``location``."""
        return self.filter(location=location)

    def for_image(self, image: Image) -> Self:
        """Every fact attached to ``image``."""
        return self.filter(image=image)

    def with_key(self, key: str) -> Self:
        """Restrict to facts of one key, regardless of subject."""
        return self.filter(key=key)

    def min_confidence(self, threshold: float) -> Self:
        """Restrict to facts at or above ``threshold`` confidence."""
        return self.filter(confidence__gte=threshold)


class FactManager(abstract.DashboardManager.from_queryset(FactQuerySet)):
    """Manager for Fact."""


class FactEvidenceQuerySet(abstract.DashboardQuerySet):
    """QuerySet for FactEvidence."""

    def for_fact(self, fact: Fact) -> Self:
        """Every evidence row logged for ``fact``, any status."""
        return self.filter(fact=fact)

    def active(self) -> Self:
        """Restrict to non-superseded evidence - what confidence recomputation reads."""
        return self.filter(superseded=False)


class FactEvidenceManager(abstract.DashboardManager.from_queryset(FactEvidenceQuerySet)):
    """Manager for FactEvidence."""
