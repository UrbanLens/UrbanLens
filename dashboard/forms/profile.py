from django import forms
from dashboard.models.profile.model import Profile

class ProfileForm(forms.ModelForm):
    class Meta:
        model = Profile
        fields = ['avatar', 'instagram', 'discord']
