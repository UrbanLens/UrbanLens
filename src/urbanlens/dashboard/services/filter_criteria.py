"""Shared (de)serialization for persisted main-map filter criteria.

``SearchForm.cleaned_data`` (``dashboard.forms.search.SearchForm``) is not
directly JSON-safe: its ``custom_fields`` criteria (from
``SearchForm.parse_custom_field_criteria()``) carry live ``CustomField``
model instances rather than ids. ``serialize_form_criteria`` normalizes a
cleaned-data-shaped dict into a plain JSON-safe dict suitable for storage on
``SavedFilter.criteria`` or ``PinList.smart_filter``; ``deserialize_criteria``
is the inverse, rehydrating a stored dict back into the shape
``Pin.objects.filter_by_criteria()`` expects.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

_SCALAR_KEYS = (
    "min_rating",
    "max_rating",
    "min_priority",
    "max_priority",
    "min_danger",
    "max_danger",
    "min_vulnerability",
    "max_vulnerability",
    "has_visits",
    "overlapping_pins",
)
_DATE_KEYS = ("visited_after", "visited_before", "created_after", "created_before")


def serialize_form_criteria(
    cleaned_data: dict[str, Any],
    badge_groups: list[dict] | None,
    custom_field_criteria: list[dict] | None,
) -> dict[str, Any]:
    """Convert cleaned SearchForm data into a JSON-safe criteria dict.

    Args:
        cleaned_data: A ``SearchForm.cleaned_data``-shaped dict.
        badge_groups: Output of ``SearchForm.parse_badge_groups()``, if any.
        custom_field_criteria: Output of ``SearchForm.parse_custom_field_criteria()``, if any.

    Returns:
        A dict safe to store directly in a JSONField (``SavedFilter.criteria``
        / ``PinList.smart_filter``); absent keys mean "no filter on that
        dimension", matching ``SearchForm``'s own semantics.
    """
    out: dict[str, Any] = {}
    for key in _SCALAR_KEYS:
        value = cleaned_data.get(key)
        if value not in (None, ""):
            out[key] = value
    for key in _DATE_KEYS:
        value = cleaned_data.get(key)
        if value is not None:
            out[key] = value.isoformat()
    if badge_groups:
        out["badge_groups"] = badge_groups
    if custom_field_criteria:
        out["custom_fields"] = [_serialize_custom_field_criterion(c) for c in custom_field_criteria]
    return out


def _serialize_custom_field_criterion(criterion: dict[str, Any]) -> dict[str, Any]:
    entry: dict[str, Any] = {"field_id": criterion["field"].pk}
    if "contains" in criterion:
        entry["contains"] = criterion["contains"]
    if "min" in criterion or "max" in criterion:
        minimum = criterion.get("min")
        maximum = criterion.get("max")
        entry["min"] = str(minimum) if minimum is not None else None
        entry["max"] = str(maximum) if maximum is not None else None
    if "after" in criterion or "before" in criterion:
        after = criterion.get("after")
        before = criterion.get("before")
        entry["after"] = after.isoformat() if after else None
        entry["before"] = before.isoformat() if before else None
    return entry


def deserialize_criteria(stored: dict[str, Any], profile: Profile) -> dict[str, Any]:
    """Rehydrate a stored criteria dict for ``Pin.objects.filter_by_criteria()``.

    Args:
        stored: A dict previously produced by ``serialize_form_criteria``.
        profile: Owner used to re-resolve ``custom_fields`` field ids scoped
            to that profile's own custom fields.

    Returns:
        A criteria dict in the live-object shape ``filter_by_criteria``
        expects (dates parsed back, custom-field ids resolved to instances).
        Custom-field entries whose field was deleted since the filter was
        saved are silently dropped.
    """
    from urbanlens.dashboard.models.custom_fields.model import CustomField

    criteria: dict[str, Any] = dict(stored)
    for key in _DATE_KEYS:
        if criteria.get(key):
            criteria[key] = date.fromisoformat(criteria[key])
    if raw_custom_fields := criteria.get("custom_fields"):
        field_ids = [c["field_id"] for c in raw_custom_fields]
        fields_by_id = {cf.pk: cf for cf in CustomField.objects.filter(pk__in=field_ids, profile=profile)}
        resolved: list[dict[str, Any]] = []
        for c in raw_custom_fields:
            field = fields_by_id.get(c["field_id"])
            if field is None:
                continue
            resolved.append({"field": field, **_deserialize_custom_field_bounds(c)})
        criteria["custom_fields"] = resolved
    return criteria


def _deserialize_custom_field_bounds(criterion: dict[str, Any]) -> dict[str, Any]:
    if "contains" in criterion:
        return {"contains": criterion["contains"]}
    if "min" in criterion or "max" in criterion:
        from decimal import Decimal

        return {
            "min": Decimal(criterion["min"]) if criterion.get("min") is not None else None,
            "max": Decimal(criterion["max"]) if criterion.get("max") is not None else None,
        }
    if "after" in criterion or "before" in criterion:
        return {
            "after": date.fromisoformat(criterion["after"]) if criterion.get("after") else None,
            "before": date.fromisoformat(criterion["before"]) if criterion.get("before") else None,
        }
    return {}
