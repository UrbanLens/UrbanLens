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

from datetime import date, time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from django.contrib.gis.geos import MultiPolygon

    from urbanlens.dashboard.models.profile.model import Profile

_TEXT_KEYS = ("name",)
_GEOM_KEYS = ("include_regions", "exclude_regions")
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
    "has_links",
    "min_detail_pins",
    "max_detail_pins",
    "security_fences",
    "security_alarms",
    "security_cameras",
    "security_security",
    "security_signs",
    "security_vps",
    "security_plywood",
    "security_locked",
)
_DATE_KEYS = (
    "visited_after",
    "visited_before",
    "created_after",
    "created_before",
    "date_built_after",
    "date_built_before",
    "date_abandoned_after",
    "date_abandoned_before",
    "last_viewed_after",
    "last_viewed_before",
)
_LABEL_LIST_KEYS = ("tags", "exclude_tags")


def serialize_form_criteria(
    cleaned_data: dict[str, Any],
    label_groups: list[dict] | None,
    custom_field_criteria: list[dict] | None,
    regions: dict[str, MultiPolygon | None] | None = None,
) -> dict[str, Any]:
    """Convert cleaned SearchForm data into a JSON-safe criteria dict.

    Args:
        cleaned_data: A ``SearchForm.cleaned_data``-shaped dict.
        label_groups: Output of ``SearchForm.parse_label_groups()``, if any.
        custom_field_criteria: Output of ``SearchForm.parse_custom_field_criteria()``, if any.
        regions: Mapping of ``"include_regions"``/``"exclude_regions"`` to a
            parsed MultiPolygon (e.g. from ``SearchForm.parse_region_geojson()``),
            or None/absent for no restriction on that dimension.

    Returns:
        A dict safe to store directly in a JSONField (``SavedFilter.criteria``
        / ``PinList.smart_filter``); absent keys mean "no filter on that
        dimension", matching ``SearchForm``'s own semantics.
    """
    from urbanlens.dashboard.services.geo.geo import geometry_to_geojson

    out: dict[str, Any] = {}
    for key in _TEXT_KEYS:
        value = (cleaned_data.get(key) or "").strip()
        if value:
            out[key] = value
    for key in _SCALAR_KEYS:
        value = cleaned_data.get(key)
        if value not in (None, ""):
            out[key] = value
    for key in _DATE_KEYS:
        value = cleaned_data.get(key)
        if value is not None:
            out[key] = value.isoformat()
    if label_groups:
        out["label_groups"] = label_groups
    else:
        # Flat tags/exclude_tags only matter as a fallback when label_groups
        # (the map formula bar's richer AND/OR/NOT logic) isn't set - storing
        # both would be dead weight since filter_by_criteria always prefers
        # label_groups when present.
        for key in _LABEL_LIST_KEYS:
            value = cleaned_data.get(key)
            if value:
                out[key] = [label.pk for label in value]
    if custom_field_criteria:
        out["custom_fields"] = [_serialize_custom_field_criterion(c) for c in custom_field_criteria]
    for key in _GEOM_KEYS:
        geom = (regions or {}).get(key)
        if geom:
            out[key] = geometry_to_geojson(geom)
    return out


def _serialize_custom_field_criterion(criterion: dict[str, Any]) -> dict[str, Any]:
    entry: dict[str, Any] = {"field_id": criterion["field"].pk}
    if "contains" in criterion:
        entry["contains"] = criterion["contains"]
    if "equals" in criterion:
        entry["equals"] = criterion["equals"]
    if "checked" in criterion:
        entry["checked"] = bool(criterion["checked"])
    if "ref_id" in criterion:
        entry["ref_id"] = int(criterion["ref_id"])
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
    if "after_time" in criterion or "before_time" in criterion:
        after_time = criterion.get("after_time")
        before_time = criterion.get("before_time")
        entry["after_time"] = after_time.isoformat() if after_time else None
        entry["before_time"] = before_time.isoformat() if before_time else None
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
    from urbanlens.dashboard.models.labels.model import Label
    from urbanlens.dashboard.services.geo.geo import parse_multipolygon_geojson

    criteria: dict[str, Any] = dict(stored)
    for key in _DATE_KEYS:
        if criteria.get(key):
            criteria[key] = date.fromisoformat(criteria[key])
    for key in _LABEL_LIST_KEYS:
        if label_ids := criteria.get(key):
            criteria[key] = list(Label.objects.filter(pk__in=label_ids))
    for key in _GEOM_KEYS:
        if raw_geom := criteria.get(key):
            try:
                criteria[key] = parse_multipolygon_geojson(raw_geom)
            except (ValueError, TypeError):
                criteria.pop(key, None)
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


class CriteriaOwnershipError(ValueError):
    """Stored criteria referenced a label or custom field the profile may not use.

    ``safe_message`` is safe to surface directly to the caller.
    """

    def __init__(self, message: str) -> None:
        self.safe_message = message
        super().__init__(message)


def referenced_label_ids(stored: dict[str, Any]) -> set[int]:
    """Every Label primary key a stored criteria dict refers to.

    Label pks appear in three places: the flat ``tags``/``exclude_tags`` lists
    and the richer ``label_groups`` formula (``[{"op": ..., "ids": [...]}]``).

    Args:
        stored: A criteria dict in the stored (JSON-safe) shape.

    Returns:
        The set of referenced label pks. Non-integer entries are ignored - they
        cannot match a real label, and this function's job is to enumerate, not
        to validate the shape.
    """
    ids: set[int] = set()
    for key in _LABEL_LIST_KEYS:
        for value in stored.get(key) or []:
            if isinstance(value, int):
                ids.add(value)
    for group in stored.get("label_groups") or []:
        if not isinstance(group, dict):
            continue
        for value in group.get("ids") or []:
            if isinstance(value, int):
                ids.add(value)
    return ids


def referenced_custom_field_ids(stored: dict[str, Any]) -> set[int]:
    """Every CustomField primary key a stored criteria dict refers to.

    Args:
        stored: A criteria dict in the stored (JSON-safe) shape.

    Returns:
        The set of referenced custom-field pks.
    """
    ids: set[int] = set()
    for criterion in stored.get("custom_fields") or []:
        if isinstance(criterion, dict) and isinstance(criterion.get("field_id"), int):
            ids.add(criterion["field_id"])
    return ids


def validate_criteria_ownership(stored: dict[str, Any], profile: Profile) -> None:
    """Refuse criteria that reference labels or custom fields *profile* may not use.

    This check does not exist on the internal write paths, and its absence is
    invisible there: the web form builds its label and custom-field pickers
    from querysets already scoped to the user, so the browser never offers an
    id the user cannot see. That is a *UI* constraint, not a data-layer one.

    An API client is under no such obligation. Without this, a caller could
    save a filter naming an arbitrary label or custom-field pk and read the
    match count back out - turning saved filters into an oracle for probing
    other users' (and global) primary-key space one id at a time. Any external
    write path accepting ``criteria`` must call this.

    ``deserialize_criteria`` silently drops unknown custom fields at *read*
    time, which prevents them from affecting results but happens far too late
    to stop the probe, and does nothing at all for label ids.

    Args:
        stored: A criteria dict in the stored (JSON-safe) shape.
        profile: The profile the criteria will belong to.

    Raises:
        CriteriaOwnershipError: If any referenced label is not visible to
            *profile*, or any referenced custom field is not theirs. The
            message deliberately does not say which id was rejected, since
            distinguishing "exists but not yours" from "does not exist" is the
            very thing being denied.
    """
    from urbanlens.dashboard.models.custom_fields.model import CustomField
    from urbanlens.dashboard.models.labels.model import Label

    label_ids = referenced_label_ids(stored)
    if label_ids:
        visible = set(Label.objects.visible_to(profile).filter(pk__in=label_ids).values_list("pk", flat=True))
        if visible != label_ids:
            raise CriteriaOwnershipError("Filter criteria reference a label that does not exist.")

    field_ids = referenced_custom_field_ids(stored)
    if field_ids:
        owned = set(CustomField.objects.filter(profile=profile, pk__in=field_ids).values_list("pk", flat=True))
        if owned != field_ids:
            raise CriteriaOwnershipError("Filter criteria reference a custom field that does not exist.")


def _deserialize_custom_field_bounds(criterion: dict[str, Any]) -> dict[str, Any]:
    if "contains" in criterion:
        return {"contains": criterion["contains"]}
    if "equals" in criterion:
        return {"equals": criterion["equals"]}
    if "checked" in criterion:
        return {"checked": bool(criterion["checked"])}
    if "ref_id" in criterion:
        return {"ref_id": int(criterion["ref_id"])}
    if "min" in criterion or "max" in criterion:
        from decimal import Decimal

        return {
            "min": Decimal(criterion["min"]) if criterion.get("min") is not None else None,
            "max": Decimal(criterion["max"]) if criterion.get("max") is not None else None,
        }
    if "after_time" in criterion or "before_time" in criterion:
        return {
            "after_time": time.fromisoformat(criterion["after_time"]) if criterion.get("after_time") else None,
            "before_time": time.fromisoformat(criterion["before_time"]) if criterion.get("before_time") else None,
        }
    if "after" in criterion or "before" in criterion:
        return {
            "after": date.fromisoformat(criterion["after"]) if criterion.get("after") else None,
            "before": date.fromisoformat(criterion["before"]) if criterion.get("before") else None,
        }
    return {}
