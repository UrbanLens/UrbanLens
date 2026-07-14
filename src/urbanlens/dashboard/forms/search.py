from __future__ import annotations

import json
from typing import TYPE_CHECKING

from django import forms

from urbanlens.dashboard.models.custom_fields.model import CustomField, CustomFieldEntity, CustomFieldType
from urbanlens.dashboard.models.labels.model import Label

if TYPE_CHECKING:
    from django.contrib.gis.geos import MultiPolygon

    from urbanlens.dashboard.models.profile.model import Profile


class SearchForm(forms.Form):
    """Filter form for map pin search.

    Fields are all optional; omitting a field means "no filter on that dimension."

    Label filtering accepts either the legacy ``tags``/``exclude_tags`` fields OR the
    richer ``label_groups`` JSON field (produced by the formula bar).  ``label_groups``
    takes precedence when present.  Its schema is a JSON array of group objects::

        [{"op": "and"|"or"|"not", "ids": [<label_id>, ...]}, ...]

    ``and``  - pin must have ALL labels in the group.
    ``or``   - pin must have AT LEAST ONE label in the group.
    ``not``  - pin must have NONE of the labels in the group.

    When constructed with a ``profile``, one form field per custom pin field is
    added dynamically (named ``cf_<id>`` for text, ``cf_<id>_min``/``_max`` for
    numbers, ``cf_<id>_after``/``_before`` for dates) so the owner can filter
    the map by their own custom field values.
    """

    name = forms.CharField(required=False)
    min_rating = forms.IntegerField(required=False, min_value=0, max_value=5)
    max_rating = forms.IntegerField(required=False, min_value=0, max_value=5)
    tags: forms.ModelMultipleChoiceField = forms.ModelMultipleChoiceField(
        queryset=Label.objects.all(),
        widget=forms.CheckboxSelectMultiple,
        required=False,
    )
    exclude_tags: forms.ModelMultipleChoiceField = forms.ModelMultipleChoiceField(
        queryset=Label.objects.all(),
        widget=forms.CheckboxSelectMultiple,
        required=False,
    )
    # Structured label query from the formula bar - supersedes tags/exclude_tags when set.
    label_groups = forms.CharField(required=False)
    has_visits = forms.ChoiceField(
        choices=[("", ""), ("yes", "yes"), ("no", "no")],
        required=False,
    )
    visited_after = forms.DateField(required=False, input_formats=["%Y-%m-%d"])
    visited_before = forms.DateField(required=False, input_formats=["%Y-%m-%d"])
    min_priority = forms.IntegerField(required=False, min_value=0)
    max_priority = forms.IntegerField(required=False, min_value=0)
    min_danger = forms.IntegerField(required=False, min_value=0, max_value=5)
    max_danger = forms.IntegerField(required=False, min_value=0, max_value=5)
    min_vulnerability = forms.IntegerField(required=False, min_value=0)
    max_vulnerability = forms.IntegerField(required=False, min_value=0)
    created_after = forms.DateField(required=False, input_formats=["%Y-%m-%d"])
    created_before = forms.DateField(required=False, input_formats=["%Y-%m-%d"])
    overlapping_pins = forms.BooleanField(required=False)
    # Raw GeoJSON MultiPolygon text (a SavedFilter's drawn/geocoded regions) - see
    # parse_region_geojson(). Not rendered as a visible field on the map's filter
    # sidebar; only carried through so applying a saved filter with regions still
    # narrows map results correctly.
    include_regions = forms.CharField(required=False)
    exclude_regions = forms.CharField(required=False)

    def __init__(self, *args, profile: Profile | None = None, **kwargs) -> None:
        """Build the form, adding one filter field per custom pin field when a profile is given.

        Args:
            *args: Standard form args (usually the request data).
            profile: The requesting user's profile; enables custom-field filters.
            **kwargs: Standard form kwargs.
        """
        super().__init__(*args, **kwargs)
        self.custom_fields: list[CustomField] = []
        visible_labels = Label.objects.visible_to(profile).ordered() if profile else Label.objects.global_only().ordered()
        tags_field = self.fields["tags"]
        exclude_tags_field = self.fields["exclude_tags"]
        if isinstance(tags_field, forms.ModelMultipleChoiceField):
            tags_field.queryset = visible_labels
        if isinstance(exclude_tags_field, forms.ModelMultipleChoiceField):
            exclude_tags_field.queryset = visible_labels
        if profile is None:
            return
        self.custom_fields = list(CustomField.objects.for_entity(profile, CustomFieldEntity.PIN))
        for cf in self.custom_fields:
            if cf.field_type == CustomFieldType.NUMBER:
                self.fields[f"cf_{cf.pk}_min"] = forms.DecimalField(required=False)
                self.fields[f"cf_{cf.pk}_max"] = forms.DecimalField(required=False)
            elif cf.field_type == CustomFieldType.DATE:
                self.fields[f"cf_{cf.pk}_after"] = forms.DateField(required=False, input_formats=["%Y-%m-%d"])
                self.fields[f"cf_{cf.pk}_before"] = forms.DateField(required=False, input_formats=["%Y-%m-%d"])
            else:
                self.fields[f"cf_{cf.pk}"] = forms.CharField(required=False)

    def parse_custom_field_criteria(self) -> list[dict] | None:
        """Collect active custom-field filters from cleaned_data.

        Returns:
            List of criteria dicts (each carrying its ``field`` plus the active
            bounds/text), or None when no custom-field filter is set. Shapes::

                {"field": cf, "contains": str}                      # text
                {"field": cf, "min": Decimal|None, "max": ...}      # number
                {"field": cf, "after": date|None, "before": ...}    # date
        """
        criteria: list[dict] = []
        for cf in self.custom_fields:
            if cf.field_type == CustomFieldType.NUMBER:
                minimum = self.cleaned_data.get(f"cf_{cf.pk}_min")
                maximum = self.cleaned_data.get(f"cf_{cf.pk}_max")
                if minimum is not None or maximum is not None:
                    criteria.append({"field": cf, "min": minimum, "max": maximum})
            elif cf.field_type == CustomFieldType.DATE:
                after = self.cleaned_data.get(f"cf_{cf.pk}_after")
                before = self.cleaned_data.get(f"cf_{cf.pk}_before")
                if after is not None or before is not None:
                    criteria.append({"field": cf, "after": after, "before": before})
            else:
                text = (self.cleaned_data.get(f"cf_{cf.pk}") or "").strip()
                if text:
                    criteria.append({"field": cf, "contains": text})
        return criteria or None

    def parse_region_geojson(self, key: str) -> MultiPolygon | None:
        """Parse ``include_regions``/``exclude_regions`` raw GeoJSON text into a MultiPolygon.

        Args:
            key: "include_regions" or "exclude_regions".

        Returns:
            The parsed MultiPolygon, or None when absent or malformed. Never
            raises - a corrupted region payload should drop that one
            criterion, not fail the whole search.
        """
        raw = (self.cleaned_data.get(key) or "").strip()
        if not raw:
            return None
        try:
            geojson = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        from urbanlens.dashboard.services.geo import parse_multipolygon_geojson

        try:
            return parse_multipolygon_geojson(geojson)
        except (ValueError, TypeError):
            return None

    def parse_label_groups(self) -> list[dict] | None:
        """Parse the ``label_groups`` JSON field into a list of group dicts.

        Returns:
            List of ``{"op": str, "ids": list[int]}`` dicts, or ``None`` when the
            field is absent or malformed.
        """
        raw = (self.cleaned_data.get("label_groups") or "").strip()
        if not raw:
            return None
        try:
            groups = json.loads(raw)
            if not isinstance(groups, list):
                return None
            validated = []
            for g in groups:
                op = g.get("op")
                ids = g.get("ids")
                if op in {"and", "or", "not"} and isinstance(ids, list):
                    validated.append({"op": op, "ids": [int(i) for i in ids if str(i).isdigit()]})
            return validated or None
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
