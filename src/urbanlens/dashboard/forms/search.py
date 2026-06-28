from __future__ import annotations

import json

from django import forms

from urbanlens.dashboard.models.badges.model import Badge


class SearchForm(forms.Form):
    """Filter form for map pin search.

    Fields are all optional; omitting a field means "no filter on that dimension."

    Badge filtering accepts either the legacy ``tags``/``exclude_tags`` fields OR the
    richer ``badge_groups`` JSON field (produced by the formula bar).  ``badge_groups``
    takes precedence when present.  Its schema is a JSON array of group objects::

        [{"op": "and"|"or"|"not", "ids": [<badge_id>, ...]}, ...]

    ``and``  — pin must have ALL badges in the group.
    ``or``   — pin must have AT LEAST ONE badge in the group.
    ``not``  — pin must have NONE of the badges in the group.
    """

    name = forms.CharField(required=False)
    min_rating = forms.IntegerField(required=False, min_value=0, max_value=5)
    max_rating = forms.IntegerField(required=False, min_value=0, max_value=5)
    tags: forms.ModelMultipleChoiceField = forms.ModelMultipleChoiceField(
        queryset=Badge.objects.all(),
        widget=forms.CheckboxSelectMultiple,
        required=False,
    )
    exclude_tags: forms.ModelMultipleChoiceField = forms.ModelMultipleChoiceField(
        queryset=Badge.objects.all(),
        widget=forms.CheckboxSelectMultiple,
        required=False,
    )
    # Structured badge query from the formula bar — supersedes tags/exclude_tags when set.
    badge_groups = forms.CharField(required=False)
    has_visits = forms.ChoiceField(
        choices=[("", ""), ("yes", "yes"), ("no", "no")],
        required=False,
    )
    visited_after = forms.DateField(required=False, input_formats=["%Y-%m-%d"])
    visited_before = forms.DateField(required=False, input_formats=["%Y-%m-%d"])
    min_priority = forms.IntegerField(required=False, min_value=0)
    max_priority = forms.IntegerField(required=False, min_value=0)
    created_after = forms.DateField(required=False, input_formats=["%Y-%m-%d"])
    created_before = forms.DateField(required=False, input_formats=["%Y-%m-%d"])

    def parse_badge_groups(self) -> list[dict] | None:
        """Parse the ``badge_groups`` JSON field into a list of group dicts.

        Returns:
            List of ``{"op": str, "ids": list[int]}`` dicts, or ``None`` when the
            field is absent or malformed.
        """
        raw = (self.cleaned_data.get("badge_groups") or "").strip()
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
