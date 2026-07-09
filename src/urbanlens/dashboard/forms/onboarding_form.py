"""Form for the first-login /welcome/ page: bulk Memories/Community/External-APIs toggles."""

from django import forms

from urbanlens.dashboard.models.profile.model import Profile


class WelcomeOnboardingForm(forms.ModelForm):
    """One checkbox per category, each defaulting to checked (fully-featured).

    ``memories_enabled`` is not itself a model field - it bulk-sets the three
    Memories sub-settings together on save(). Those three remain independently
    adjustable afterward from the settings page; this form only offers the
    bulk "all on/all off" choice for a quick first impression.
    """

    memories_enabled = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="Memories",
        help_text="Save your visit history, GPS routes, and live-location-based visits.",
    )
    community_enabled = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="Community",
        help_text="Let other users see your pins, view your profile, and send/receive friend requests.",
    )
    external_apis_enabled = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="External Services",
        help_text="Allow UrbanLens to call external services (weather, geocoding, place data, AI) on your behalf.",
    )

    class Meta:
        model = Profile
        fields = ["community_enabled", "external_apis_enabled"]

    def save(self, commit: bool = True) -> Profile:
        """Apply the bulk toggles, cascading External APIs off to its existing sub-toggles.

        Community's own cascade (pin/visibility forcing) happens in
        ``Profile.save()`` itself, so nothing extra is needed for it here.
        """
        instance = super().save(commit=False)

        memories_enabled = self.cleaned_data["memories_enabled"]
        instance.track_pin_visits = memories_enabled
        instance.track_routes = memories_enabled
        instance.track_geolocation = memories_enabled

        if not instance.external_apis_enabled:
            instance.places_google_enabled = False
            instance.places_nps_enabled = False
            instance.places_wikipedia_enabled = False
            instance.ai_enabled = False

        if commit:
            instance.save()
        return instance
