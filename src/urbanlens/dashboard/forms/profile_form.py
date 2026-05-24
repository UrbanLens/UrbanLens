"""Profile info and social-link forms."""

from datetime import UTC, date, datetime

from django import forms

from urbanlens.dashboard.models.profile.model import Profile

_MIN_AGE_YEARS = 13


def _today() -> date:
    return datetime.now(tz=UTC).date()


def validate_birth_date(value: date | None) -> str | None:
    """Return an error string if invalid, otherwise None."""
    if value is None:
        return None
    today = _today()
    if value > today:
        return "Birth date cannot be in the future."
    try:
        min_date = today.replace(year=today.year - _MIN_AGE_YEARS)
    except ValueError:
        min_date = today.replace(year=today.year - _MIN_AGE_YEARS, day=28)
    if value > min_date:
        return f"You must be at least {_MIN_AGE_YEARS} years old to use this service."
    return None


def validate_started_exploring(value: date | None) -> str | None:
    """Return an error string if invalid, otherwise None."""
    if value is None:
        return None
    if value > _today():
        return "Exploring since date cannot be in the future."
    return None


class ProfileForm(forms.ModelForm):
    """Bio, location, and dates - social links are managed separately."""

    class Meta:
        model = Profile
        fields = ["avatar", "bio", "area", "birth_date", "started_exploring"]
        widgets = {
            "bio": forms.Textarea(attrs={"rows": 4, "class": "edit-textarea", "data-autosave": "bio"}),
            "area": forms.TextInput(attrs={"class": "edit-input", "data-autosave": "area"}),
            "birth_date": forms.DateInput(attrs={"type": "date", "class": "edit-input", "data-autosave": "birth_date"}),
            "started_exploring": forms.DateInput(
                attrs={"type": "date", "class": "edit-input", "data-autosave": "started_exploring"},
            ),
        }

    def clean_birth_date(self):
        value = self.cleaned_data.get("birth_date")
        error = validate_birth_date(value)
        if error:
            raise forms.ValidationError(error)
        return value

    def clean_started_exploring(self):
        value = self.cleaned_data.get("started_exploring")
        error = validate_started_exploring(value)
        if error:
            raise forms.ValidationError(error)
        return value


class DiscordHandleForm(forms.Form):
    """Discord does not have a public profile URL; handle is entered directly."""

    discord = forms.CharField(
        max_length=100,
        required=False,
        widget=forms.TextInput(
            attrs={"class": "edit-input", "placeholder": "Your Discord username"},
        ),
    )
