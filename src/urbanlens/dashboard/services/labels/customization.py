"""Per-profile display overrides for labels (``LabelCustomization``).

A customization is how a user renames or re-styles a label they do not own -
in practice a global label, which they cannot edit because everyone shares it.
Each of ``name``/``icon``/``color`` is independently nullable: null means "use
the label's own value", non-null means "override it".

Extracted from ``controllers.labels.LabelCustomizeView.post`` so the external
API applies the same normalization, the same delete-when-empty rule, and the
same map-cache nudge the web UI does.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

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

    Empty strings normalize to None, so "" and null both mean "no override" -
    there is deliberately no third state. When all three end up None the row is
    deleted rather than stored as an all-null record, keeping "customized"
    equivalent to "row exists and carries something", which is what
    ``Label.is_customized`` assumes.

    Pins are touched afterwards for the same reason
    ``LabelCustomizeView.post`` does it: a customization changes how this
    profile's pins render on the map without writing to any Pin row, so the
    map cache's freshness check needs an explicit nudge or it will keep
    serving the old styling.

    Args:
        profile: The profile whose overrides these are.
        label: The label being customized. Any label the profile can see is
            valid - this is the only way a client can restyle a global label.
        name: Display-name override, or None/"" to clear it.
        icon: Icon override, or None/"" to clear it.
        color: Color override, or None/"" to clear it.

    Returns:
        The stored :class:`LabelCustomization`, or None when every override was
        empty and any existing row was therefore deleted.
    """
    from urbanlens.dashboard.models.labels.customization import LabelCustomization
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.core import icons
    from urbanlens.dashboard.services.core.text_limits import column_max_length

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

    Pin.objects.filter(profile=profile, labels=label).update(updated=timezone.now())
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
    from urbanlens.dashboard.models.pin.model import Pin

    deleted, _ = LabelCustomization.objects.filter(profile=profile, label=label).delete()
    Pin.objects.filter(profile=profile, labels=label).update(updated=timezone.now())
    return bool(deleted)
