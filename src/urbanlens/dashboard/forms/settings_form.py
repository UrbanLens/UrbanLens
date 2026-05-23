"""User settings form - privacy, contact, and style preferences."""

from django import forms

from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice


class PrivacySettingsForm(forms.ModelForm):
    """Controls who can see this user's profile and comments, and whether they accept friend requests."""

    profile_visibility = forms.ChoiceField(
        choices=VisibilityChoice.choices,
        widget=forms.Select(attrs={"class": "settings-select"}),
        label="Profile Visibility",
        help_text="Who can view your profile page.",
    )
    comment_visibility = forms.ChoiceField(
        choices=VisibilityChoice.choices,
        widget=forms.Select(attrs={"class": "settings-select"}),
        label="Comment Visibility",
        help_text="Who can see your comments on locations.",
    )
    allow_friend_requests = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "settings-checkbox"}),
        label="Allow Friend Requests",
        help_text="When disabled, other users cannot send you friend requests.",
    )

    class Meta:
        model = Profile
        fields = ["profile_visibility", "comment_visibility", "allow_friend_requests"]


class ContactSettingsForm(forms.Form):
    """Contact information — saves to the Django User model."""

    email = forms.EmailField(
        label="Email Address",
        help_text="Used for account recovery and notifications.",
        widget=forms.EmailInput(attrs={"class": "settings-input", "autocomplete": "email"}),
    )


class StyleSettingsForm(forms.ModelForm):
    """Display preferences saved on the Profile."""

    dark_mode = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "settings-checkbox"}),
        label="Dark Mode",
        help_text="Use a dark color scheme across the app.",
    )

    class Meta:
        model = Profile
        fields = ["dark_mode"]
