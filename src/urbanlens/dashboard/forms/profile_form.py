"""Profile info and social-link forms."""

from datetime import UTC, date, datetime
import re

from django import forms

from urbanlens.dashboard.models.profile.model import Profile

# Discord usernames (new pomelo system): 2-32 chars, letters/digits/underscores/dots.
# Also allow hyphens and '#' so legacy discriminator-style handles (user#1234) remain valid.
_DISCORD_HANDLE_RE = re.compile(r"^[a-zA-Z0-9._#-]{2,100}$")

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


#: Interaction-preference choice fields, each paired with a free-text
#: "<field>_other" companion - see Profile.PREFERENCE_FIELDS for the same
#: pairing used to display the answers on the public profile page.
_PREFERENCE_SELECT_FIELDS = [field for field, _label in Profile.PREFERENCE_FIELDS]
_PREFERENCE_OTHER_FIELDS = [f"{field}_other" for field in _PREFERENCE_SELECT_FIELDS]


class ProfileForm(forms.ModelForm):
    """Bio, location, dates, and interaction preferences - social links are managed separately.

    Every field here is public presentation: what other users see on this
    profile's page, not a technical setting - see the module docstring on
    ``external_api.serializers.ProfileUpdateSerializer`` for why that split
    matters for the mobile API surface.
    """

    class Meta:
        model = Profile
        fields = [
            "avatar",
            "bio",
            "area",
            "birth_date",
            "started_exploring",
            *_PREFERENCE_SELECT_FIELDS,
            *_PREFERENCE_OTHER_FIELDS,
            "additional_preferences",
        ]
        widgets = {
            "bio": forms.Textarea(attrs={"rows": 4, "class": "edit-textarea", "data-autosave": "bio"}),
            "area": forms.TextInput(attrs={"class": "edit-input", "data-autosave": "area"}),
            "birth_date": forms.DateInput(attrs={"type": "date", "class": "edit-input", "data-autosave": "birth_date"}),
            "started_exploring": forms.DateInput(
                attrs={"type": "date", "class": "edit-input", "data-autosave": "started_exploring"},
            ),
            **{field: forms.Select(attrs={"class": "edit-select browser-default", "data-autosave": field, "data-preference-toggle": field}) for field in _PREFERENCE_SELECT_FIELDS},
            **{
                field: forms.TextInput(attrs={"class": "edit-input", "data-autosave": field, "placeholder": "Describe your preference", "data-preference-other-for": field.removesuffix("_other")})
                for field in _PREFERENCE_OTHER_FIELDS
            },
            "additional_preferences": forms.Textarea(attrs={"rows": 3, "class": "edit-textarea", "data-autosave": "additional_preferences"}),
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

    def clean_discord(self) -> str:
        """Validate the Discord username against the allowed character set.

        Returns:
            The stripped handle, or an empty string when blank.

        Raises:
            forms.ValidationError: When the handle contains disallowed characters
                or falls outside the allowed length range.
        """
        value = self.cleaned_data.get("discord", "").strip()
        if value and not _DISCORD_HANDLE_RE.match(value):
            raise forms.ValidationError(
                "2-100 characters: letters, digits, underscores, dots, hyphens, or #.",
            )
        return value
