"""QuerySets/Managers for Fact and FactEvidence (only scope/fetch rows; logic lives in services.facts)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.facts.model import Fact, FactEvidence  # noqa: F401 - mypy needs these; ruff does not
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.wiki.model import Wiki


class FactQuerySet(abstract.DashboardQuerySet["Fact"]):
    """QuerySet for Fact."""

    def for_wiki(self, wiki: Wiki) -> Self:
        """Every fact attached to ``wiki``."""
        return self.filter(wiki=wiki)

    def for_location(self, location: Location) -> Self:
        """Every fact attached to ``location``."""
        return self.filter(location=location)

    def min_confidence(self, threshold: float) -> Self:
        """Restrict to facts at or above ``threshold`` confidence."""
        return self.filter(confidence__gte=threshold)


_FactManagerBase = abstract.DashboardManager.from_queryset(FactQuerySet)


class FactManager(_FactManagerBase):
    """Manager for Fact."""


class FactEvidenceQuerySet(abstract.DashboardQuerySet["FactEvidence"]):
    """QuerySet for FactEvidence."""

    def active(self) -> Self:
        """Restrict to non-superseded evidence - what confidence recomputation reads."""
        return self.filter(superseded=False)


_FactEvidenceManagerBase = abstract.DashboardManager.from_queryset(FactEvidenceQuerySet)


class FactEvidenceManager(_FactEvidenceManagerBase):
    """Manager for FactEvidence."""
