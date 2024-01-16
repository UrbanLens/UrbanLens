"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    search.py                                                                                            *
*        Path:    /dashboard/forms/search.py                                                                           *
*        Project: urbanlens                                                                                            *
*        Version: 1.0.0                                                                                                *
*        Created: 2024-01-16                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@manlyphotos.com                                                                                 *
*        Copyright (c) 2024 Urban Lens                                                                                 *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2024-01-16     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""
from django import forms
from dashboard.models.locations.model import Location, LocationStatus
from dashboard.models.categories.model import Category
from dashboard.models.tags.model import Tag
from django.db.models import Q

class SearchForm(forms.Form):
    name = forms.CharField(required=False)
    rating = forms.IntegerField(required=False, min_value=0, max_value=5)
    '''
    categories = forms.ModelMultipleChoiceField(
        queryset=Category.objects.all(),
        widget=forms.CheckboxSelectMultiple,
        required=False
    )
    '''
    tags = forms.ModelMultipleChoiceField(
        queryset=Tag.objects.all(),
        widget=forms.CheckboxSelectMultiple,
        required=False
    )
    status = forms.ChoiceField(choices=LocationStatus.choices, required=False)

    def get_search_query(self):
        query = Q()
        if self.cleaned_data['name']:
            query &= Q(name__icontains=self.cleaned_data['name'])
        if self.cleaned_data['rating']:
            query &= Q(review__rating=self.cleaned_data['rating'])
        if self.cleaned_data['categories']:
            query &= Q(categories__in=self.cleaned_data['categories'])
        if self.cleaned_data['tags']:
            query &= Q(tags__in=self.cleaned_data['tags'])
        if self.cleaned_data['status']:
            query &= Q(status=self.cleaned_data['status'])
        return Location.objects.filter(query)
