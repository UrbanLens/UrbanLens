"""User settings form - privacy, contact, and style preferences."""

from django import forms

from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice

_VISIBILITY_WIDGET = forms.Select(attrs={"class": "settings-select"})
_CHECKBOX_WIDGET = forms.CheckboxInput(attrs={"class": "settings-checkbox"})


class PrivacySettingsForm(forms.ModelForm):
    """Controls who can see this user's profile and comments, and whether they accept friend requests."""

    profile_visibility = forms.ChoiceField(
        choices=VisibilityChoice.choices,
        widget=_VISIBILITY_WIDGET,
        label="Profile Visibility",
        help_text="Who can view your profile page.",
    )
    comment_visibility = forms.ChoiceField(
        choices=VisibilityChoice.choices,
        widget=_VISIBILITY_WIDGET,
        label="Comment Visibility",
        help_text="Who can see your comments on locations.",
    )
    allow_friend_requests = forms.BooleanField(
        required=False,
        widget=_CHECKBOX_WIDGET,
        label="Allow Friend Requests",
        help_text="When disabled, other users cannot send you friend requests.",
    )

    class Meta:
        model = Profile
        fields = ["profile_visibility", "comment_visibility", "allow_friend_requests"]
