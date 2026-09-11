"""Per-profile display overrides for labels (``LabelCustomization``).
A customization is how a user renames or re-styles a label they do not own - in practice a global label, which they cannot edit because everyone shares it."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db import transaction

if TYPE_CHECKING:
    from urbanlens.dashboard.models.labels.customization import LabelCustomization
    from urbanlens.dashboard.models.labels.model import Label
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)


def _normalize(value: str | None) -> str | None:
    """Collapse an empty or whitespace-only override to None ("no override").

    Args:
        value: The submitted override, if any.

    Returns:
        The stripped value, or None when it carries no content.
    """
    if value is None:
        return None
    return value.strip() or None


@transaction.atomic
def upsert_label_customization(
    profile: Profile,
    label: Label,
    *,
    name: str | None = None,
    icon: str | None = None,
    color: str | None = None,
) -> LabelCustomization | None:
    """Create, update, or clear *profile*'s display overrides for *label*.
    When all three end up None the row is deleted rather than stored as an all-null record, keeping "customized" equivalent to "row exists and carries something", which is what ``Label.is_customized`` assumes.

    Args:
        profile: The profile whose overrides these are.
        label: The label being customized. Any label the profile can see is
            valid - this is the only way a client can restyle a global label.
        name: Display-name override, or None/"" to clear it.
        icon: Icon override, or None/"" to clear it.
        color: Color override, or None/"" to clear it.

    Returns:
        The stored :class:`LabelCustomization`, or None when every override was
        empty and any existing row was therefore deleted."""
    from urbanlens.dashboard.models.labels.customization import LabelCustomization
    from urbanlens.dashboard.services.core import icons
    from urbanlens.dashboard.services.core.text_limits import column_max_length
    from urbanlens.dashboard.services.map_pins.touch import touch_pins_for_label_customization

    clean_name = _normalize(name)
    # Through the shared validator, not just _normalize: this writes to a
    # 50-wide column, and an unvalidated override reached it as a DataError.
    clean_icon = icons.clean_icon(_normalize(icon), max_length=column_max_length(LabelCustomization, "icon"))
    clean_color = _normalize(color)

    if clean_name is None and clean_icon is None and clean_color is None:
        LabelCustomization.objects.filter(profile=profile, label=label).delete()
        customization = None
    else:
        customization, _created = LabelCustomization.objects.update_or_create(
            profile=profile,
            label=label,
            defaults={"name": clean_name, "icon": clean_icon, "color": clean_color},
        )

    if customization is None:
        # The delete branch above fires no post_save, so the receiver that
        # normally handles this never runs.
        touch_pins_for_label_customization(profile.pk, label.pk)
    return customization


@transaction.atomic
def clear_label_customization(profile: Profile, label: Label) -> bool:
    """Remove *profile*'s overrides for *label*, restoring the label's own styling.

    Args:
        profile: The profile whose overrides to clear.
        label: The label to restore.

    Returns:
        True if a customization row existed and was deleted.
    """
    from urbanlens.dashboard.models.labels.customization import LabelCustomization
    from urbanlens.dashboard.services.map_pins.touch import touch_pins_for_label_customization

    deleted, _ = LabelCustomization.objects.filter(profile=profile, label=label).delete()
    # A delete fires no post_save, so nothing else drops the cached pins that
    # were drawing this override, or tells the client they changed.
    touch_pins_for_label_customization(profile.pk, label.pk)
    return bool(deleted)
