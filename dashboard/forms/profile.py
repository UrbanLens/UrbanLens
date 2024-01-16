from django import forms
from dashboard.models.profile.model import Profile
from dashboard.models.categories.model import Category
from dashboard.models.tags.model import Tag
from django.db.models import Q


RATING_CHOICES = [(i, str(i)) for i in range(1, 6)]  # Assuming the rating is from 1 to 5

class ProfileForm(forms.ModelForm):
    class Meta:
        model = Profile
        fields = ['avatar', 'instagram', 'discord']


class SearchForm(forms.Form):
    name = forms.CharField(required=False)
    rating = forms.ChoiceField(choices=RATING_CHOICES, required=False)
    categories = forms.ModelMultipleChoiceField(
        queryset=Category.objects.all(),
        widget=forms.CheckboxSelectMultiple,
        required=False
    )
    tags = forms.ModelMultipleChoiceField(
        queryset=Tag.objects.all(),
        widget=forms.CheckboxSelectMultiple,
        required=False
    )

    def search(self):
        query = Q()
        if self.cleaned_data['name']:
            query &= Q(name__icontains=self.cleaned_data['name'])
        # Add additional filters based on rating, categories, and tags
        # ...
        return query
