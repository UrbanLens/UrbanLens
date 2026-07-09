"""Form for the first-login /welcome/ page: bulk Memories/Community/External-APIs disable toggles."""

from django import forms

from urbanlens.dashboard.models.profile.model import Profile


class WelcomeOnboardingForm(forms.ModelForm):
    """One checkbox per category, phrased as "disable X" and unchecked by default (fully-featured).

    None of the three fields are bound to ``Profile`` via ``Meta.fields`` - each is inverted and
    bulk-applied to several underlying settings on ``save()``. Those settings remain independently
    adjustable afterward from the settings page (where they're phrased in "enabled" terms); this
    form only offers the bulk "leave on/turn off" choice for a quick first impression.
    """

    disable_memories = forms.BooleanField(
        required=False,
        initial=False,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="Memories",
        help_text="Disable saving any data about your activity. This will turn off your visit history journal, and the Memories page. The site will refuse to import visit or location data you upload. Metadata for photos you upload will be stripped.",
    )
    disable_community = forms.BooleanField(
        required=False,
        initial=False,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="Community",
        help_text="Disable all interactions with other users. You will not able to see community wikis, join trips, or send friend requests. You will be invisible.",
    )
    disable_external_apis = forms.BooleanField(
        required=False,
        initial=False,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="External Services",
        help_text="Disable all external services (weather, geocoding, place data, web searches, AI). When viewing your pins, no research data will be displayed, unless it has already been cached from another user's request. This will prevent you from getting alerts about places you plan to visit.",
    )

    class Meta:
        model = Profile
        fields: list[str] = []

    def save(self, commit: bool = True) -> Profile:
        """Invert the bulk disable toggles onto their underlying "enabled" settings.

        Community's own cascade (pin/visibility forcing) happens in
        ``Profile.save()`` itself, so nothing extra is needed for it here.
        """
        instance = super().save(commit=False)

        memories_enabled = not self.cleaned_data["disable_memories"]
        instance.track_pin_visits = memories_enabled
        instance.track_routes = memories_enabled
        instance.track_geolocation = memories_enabled

        instance.community_enabled = not self.cleaned_data["disable_community"]

        instance.external_apis_enabled = not self.cleaned_data["disable_external_apis"]
        if not instance.external_apis_enabled:
            instance.places_google_enabled = False
            instance.places_nps_enabled = False
            instance.places_wikipedia_enabled = False
            instance.ai_enabled = False

        if commit:
            instance.save()
        return instance
