"""User settings form - privacy, contact, and style preferences."""

from django import forms

from urbanlens.dashboard.models.profile.model import MapViewChoice, Profile, VisibilityChoice

class PrivacySettingsForm(forms.ModelForm):
    """Controls who can see this user's profile and comments, and whether they accept friend requests."""

    profile_visibility = forms.ChoiceField(
        choices=VisibilityChoice.choices,
        widget=forms.Select(attrs={"class": "settings-select browser-default"}),
        label="Profile Visibility",
        help_text="Who can view your profile page.",
    )
    comment_visibility = forms.ChoiceField(
        choices=VisibilityChoice.choices,
        widget=forms.Select(attrs={"class": "settings-select browser-default"}),
        label="Comment Visibility",
        help_text="Who can see your comments on locations.",
    )
    friend_request_visibility = forms.ChoiceField(
        choices=VisibilityChoice.choices,
        widget=forms.Select(attrs={"class": "settings-select browser-default"}),
        label="Who Can Send Friend Requests",
        help_text="Control which users are allowed to send you friend requests.",
    )

    class Meta:
        model = Profile
        fields = ["profile_visibility", "comment_visibility", "friend_request_visibility"]


class ContactSettingsForm(forms.Form):
    """Contact information - saves to the Django User model."""

    email = forms.EmailField(
        label="Email Address",
        help_text="Used for account recovery and notifications.",
        widget=forms.EmailInput(
            attrs={
                "class": "settings-input",
                "autocomplete": "email",
                "placeholder": "your@email.com",
            },
        ),
    )


class StyleSettingsForm(forms.ModelForm):
    """Display preferences saved on the Profile."""

    dark_mode = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "settings-checkbox"}),
        label="Dark Mode",
        help_text="Use a dark color scheme across the app.",
    )
    default_map_view = forms.ChoiceField(
        choices=MapViewChoice.choices,
        widget=forms.RadioSelect(attrs={"class": "settings-radio"}),
        label="Default Map View",
        help_text="Which map layer to use by default on location pages.",
    )
    cluster_radius = forms.IntegerField(
        required=False,
        min_value=1,
        max_value=500,
        widget=forms.NumberInput(attrs={"class": "settings-input", "placeholder": "Auto (zoom-based)"}),
        label="Cluster Radius",
        help_text="Pixels within which nearby pins are grouped into a cluster. Leave blank to use the automatic zoom-based value.",
    )

    class Meta:
        model = Profile
        fields = ["dark_mode", "default_map_view", "cluster_radius"]
