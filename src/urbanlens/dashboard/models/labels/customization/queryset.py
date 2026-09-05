"""LabelCustomization queryset and manager."""

from __future__ import annotations

from urbanlens.dashboard.models import abstract


class LabelCustomizationQuerySet(abstract.DashboardQuerySet):
    """QuerySet for per-user label display overrides."""

    def bulk_create(self, objs, *args, **kwargs):
        """Create overrides in bulk, coercing each colour first.

        ``bulk_create`` does not call ``save()``, so the model's coercion has to
        be repeated here or a bulk path stores what a single write would reject.

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
        return super().bulk_create(objs, *args, **kwargs)


class LabelCustomizationManager(abstract.DashboardManager.from_queryset(LabelCustomizationQuerySet)):
    """Manager for LabelCustomization records."""
