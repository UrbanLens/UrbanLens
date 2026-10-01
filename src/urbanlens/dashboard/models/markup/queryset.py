"""Querysets and managers for MarkupMap and PinMarkup."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.markup.model import CustomLayer, MarkupMap, PinMarkup  # noqa: F401 - mypy needs these; ruff does not

logger = logging.getLogger(__name__)


class PinMarkupQuerySet(abstract.FrontendDashboardQuerySet["PinMarkup"]):
    """QuerySet for PinMarkup map annotations (lines, arrows, text labels)."""

    def bulk_create(self, objs, *args, **kwargs):
        """Create the items, coercing their colours the way ``save`` would.
        ``bulk_create`` issues raw SQL and never calls ``save``, so the model-level colour validation - the thing standing between a stored string and the client's ``innerHTML`` - does not apply to it.

        Args:
            objs: The items to create.
            *args: Passed through to Django's ``bulk_create``.
            **kwargs: Passed through to Django's ``bulk_create``.

        Returns:
            The created items, as Django's ``bulk_create`` returns them.
        """
        objs = list(objs)
        for obj in objs:
            obj.coerce_colors()
        return super().bulk_create(objs, *args, **kwargs)

    def for_pin(self, pin) -> Self:
        """All markup items belonging to a specific parent pin."""
        return self.filter(parent_pin=pin)

    def for_wiki(self, wiki) -> Self:
        """All shared/community markup items belonging to a specific Wiki."""
        return self.filter(parent_wiki=wiki)

    def for_map(self, markup_map) -> Self:
        """All markup items belonging to a specific MarkupMap."""
        return self.filter(parent_map=markup_map)

    def for_profile(self, profile) -> Self:
        """All markup items belonging to a specific profile."""
        return self.filter(profile=profile)


_PinMarkupManagerBase = abstract.FrontendDashboardManager.from_queryset(PinMarkupQuerySet)


class PinMarkupManager(_PinMarkupManagerBase):
    """Manager for PinMarkup."""


class MarkupMapQuerySet(abstract.FrontendDashboardQuerySet["MarkupMap"]):
    """QuerySet for standalone MarkupMap containers."""

    def for_profile(self, profile) -> Self:
        """All markup maps owned by a specific profile."""
        return self.filter(profile=profile)

    def cloned_from(self, source) -> Self:
        """Clones of a specific source map (used to check for an existing clone)."""
        return self.filter(cloned_from=source)

    def unattached(self) -> Self:
        """Maps not linked from any host model (drafts / leftovers).

        Returns:
            Maps with no safety check-in, comment, trip comment, or visit
            pointing at them.
        """
        return self.filter(
            safety_checkins__isnull=True,
            comments__isnull=True,
            trip_comments__isnull=True,
            visits__isnull=True,
        )


_MarkupMapManagerBase = abstract.FrontendDashboardManager.from_queryset(MarkupMapQuerySet)


class MarkupMapManager(_MarkupMapManagerBase):
    """Manager for MarkupMap."""


class CustomLayerQuerySet(abstract.FrontendDashboardQuerySet["CustomLayer"]):
    """QuerySet for CustomLayer (per-pin/wiki groupings of markup items)."""

    def for_pin(self, pin) -> Self:
        """All custom layers belonging to a specific parent pin."""
        return self.filter(parent_pin=pin)

    def for_wiki(self, wiki) -> Self:
        """All shared/community custom layers belonging to a specific Wiki."""
        return self.filter(parent_wiki=wiki)


_CustomLayerManagerBase = abstract.FrontendDashboardManager.from_queryset(CustomLayerQuerySet)


class CustomLayerManager(_CustomLayerManagerBase):
    """Manager for CustomLayer."""
