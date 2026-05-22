"""Profile info and social-link forms."""

from django import forms

from urbanlens.dashboard.models.profile.model import Profile


class ProfileForm(forms.ModelForm):
    """Bio, location, and dates - social links are managed separately."""

    class Meta:
        model = Profile
        fields = [
            "avatar",
            "bio",
            "area",
            "birth_date",
            "started_exploring",
        ]
        widgets = {
            "bio": forms.Textarea(attrs={"rows": 4, "class": "edit-textarea"}),
            "area": forms.TextInput(attrs={"class": "edit-input"}),
            "birth_date": forms.DateInput(attrs={"type": "date", "class": "edit-input"}),
            "started_exploring": forms.DateInput(attrs={"type": "date", "class": "edit-input"}),
        }


class DiscordHandleForm(forms.Form):
    """Discord does not have a public profile URL; handle is entered directly."""

    discord = forms.CharField(
        max_length=100,
        required=False,
        widget=forms.TextInput(
            attrs={"class": "edit-input", "placeholder": "Your Discord username"},
        ),
    )
