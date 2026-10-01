"""LabelCustomization queryset and manager."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.labels.customization.model import LabelCustomization  # noqa: F401 - mypy needs these; ruff does not


class LabelCustomizationQuerySet(abstract.DashboardQuerySet["LabelCustomization"]):
    """QuerySet for per-user label display overrides."""

    def bulk_create(self, objs, *args, **kwargs):
        """Create overrides in bulk, coercing each colour first.
        ``bulk_create`` does not call ``save()``, so the model's coercion has to be repeated here or a bulk path stores what a single write would reject.

        Args:
            objs: The overrides to create.
            *args: Passed through to Django's ``bulk_create``.
            **kwargs: Passed through to Django's ``bulk_create``.

        Returns:
            The created overrides, as Django's ``bulk_create`` returns them.
        """
        objs = list(objs)
        for obj in objs:
            obj.coerce_colors()
            obj.coerce_icon()
        return super().bulk_create(objs, *args, **kwargs)

    def bulk_update(self, objs, fields, *args, **kwargs):
        """Update overrides in bulk, coercing each colour first.

        Args:
            objs: The overrides to update.
            fields: The column names to write.
            *args: Passed through to Django's ``bulk_update``.
            **kwargs: Passed through to Django's ``bulk_update``.

        Returns:
            Whatever Django's ``bulk_update`` returns.
        """
        objs = list(objs)
        for obj in objs:
            if "color" in fields:
                obj.coerce_colors()
            if "icon" in fields:
                obj.coerce_icon()
        return super().bulk_update(objs, fields, *args, **kwargs)


_LabelCustomizationManagerBase = abstract.DashboardManager.from_queryset(LabelCustomizationQuerySet)


class LabelCustomizationManager(_LabelCustomizationManagerBase):
    """Manager for LabelCustomization records."""
