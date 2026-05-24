from django import forms

from urbanlens.dashboard.models.pin.model import PinStatus
from urbanlens.dashboard.models.tags.model import Tag


class SearchForm(forms.Form):
    """Filter form for map pin search.

    Fields are all optional; omitting a field means "no filter on that dimension."
    """

    name = forms.CharField(required=False)
    min_rating = forms.IntegerField(required=False, min_value=0, max_value=5)
    max_rating = forms.IntegerField(required=False, min_value=0, max_value=5)
    tags: forms.ModelMultipleChoiceField = forms.ModelMultipleChoiceField(
        queryset=Tag.objects.all(),
        widget=forms.CheckboxSelectMultiple,
        required=False,
    )
    status = forms.MultipleChoiceField(
        choices=PinStatus.choices,
        widget=forms.CheckboxSelectMultiple,
        required=False,
    )
    has_visits = forms.ChoiceField(
        choices=[("", ""), ("yes", "yes"), ("no", "no")],
        required=False,
    )
    min_priority = forms.IntegerField(required=False, min_value=0)
    created_after = forms.DateField(required=False, input_formats=["%Y-%m-%d"])
    created_before = forms.DateField(required=False, input_formats=["%Y-%m-%d"])
