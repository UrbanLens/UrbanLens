"""QuerySets and managers for custom fields and their values."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Self

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.custom_fields.model import CustomField, CustomFieldValue  # noqa: F401 - mypy needs these; ruff does not
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)


class CustomFieldQuerySet(abstract.FrontendDashboardQuerySet["CustomField"]):
    """Query helpers for :class:`~urbanlens.dashboard.models.custom_fields.model.CustomField`."""

    def owned_by(self, profile: Profile) -> Self:
        """Fields belonging to the given profile.

        Args:
            profile: The owning profile.

        Returns:
            Filtered queryset.
        """
        return self.filter(profile=profile)

    def for_entity(self, profile: Profile, entity_type: str) -> Self:
        """The given profile's fields for one entity type, in display order.

        Args:
            profile: The owning profile.
            entity_type: A :class:`CustomFieldEntity` value.

        Returns:
            Filtered queryset ordered by (order, name).
        """
        return self.filter(profile=profile, entity_type=entity_type).order_by("order", "name")


_CustomFieldManagerBase = abstract.FrontendDashboardManager.from_queryset(CustomFieldQuerySet)


class CustomFieldManager(_CustomFieldManagerBase):
    """Manager for CustomField."""


class CustomFieldValueQuerySet(abstract.DashboardQuerySet["CustomFieldValue"]):
    """Query helpers for :class:`~urbanlens.dashboard.models.custom_fields.model.CustomFieldValue`."""

    def for_target(self, target: Any) -> Self:
        """Values attached to the given target object.

        Args:
            target: A Pin, Image, Profile, or MarkupMap instance.

        Returns:
            Filtered queryset (empty for unsupported target types).
        """
        from urbanlens.dashboard.models.images.model import Image
        from urbanlens.dashboard.models.markup.model import MarkupMap
        from urbanlens.dashboard.models.pin.model import Pin
        from urbanlens.dashboard.models.profile.model import Profile as ProfileModel

        if isinstance(target, Pin):
            return self.filter(pin=target)
        if isinstance(target, Image):
            return self.filter(image=target)
        if isinstance(target, ProfileModel):
            return self.filter(target_profile=target)
        if isinstance(target, MarkupMap):
            return self.filter(markup_map=target)
        logger.warning("CustomFieldValue.for_target called with unsupported type %s", type(target).__name__)
        return self.none()


_CustomFieldValueManagerBase = abstract.DashboardManager.from_queryset(CustomFieldValueQuerySet)


class CustomFieldValueManager(_CustomFieldValueManagerBase):
    """Manager for CustomFieldValue."""
