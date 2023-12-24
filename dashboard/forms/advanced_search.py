from django import forms

class AdvancedSearchForm(forms.Form):
    date_added = forms.DateField(required=False)
    popularity = forms.IntegerField(required=False)
    tags = forms.CharField(required=False)
