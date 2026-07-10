"""Form for the first-login /welcome/ page: bulk Memories/Community/External-APIs toggles."""

from django import forms
from django.utils import timezone

from urbanlens.dashboard.models.profile.model import Profile


class WelcomeOnboardingForm(forms.ModelForm):
    """One checkbox per feature category, plus a required Terms of Service agreement.

    The three category checkboxes are each checked (fully-featured) by default; unchecking one
    disables that category, and the label/help text/tooltip copy is written from that "turn this
    off to disable it" angle. ``tos_agreed`` is the exception - it defaults unchecked, since
    agreement has to be an explicit action rather than something left on by default. None of the
    four fields are bound to ``Profile`` via ``Meta.fields`` - each is bulk-applied to one or more
    underlying settings on ``save()``. The category settings remain independently adjustable
    afterward from the settings page; this form only offers the bulk "leave on/turn off" choice
    for a quick first impression.
    """

    memories_enabled = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="Memories",
        help_text="Disable features that allow you to record your activities. This will turn off your visit journal and the Memories page. The site will refuse to import visit or location data you upload. Metadata for photos you upload will be stripped.",
    )
    community_enabled = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="Community",
        help_text="Disable all interactions with other users. You will not able to see community wikis, join trips, or send friend requests. You will be invisible.",
    )
    external_apis_enabled = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="External Services",
        help_text="Disable all external services (weather, geocoding, place data, web searches, AI). No research data will be displayed, unless it has already been cached from another user's request. This will prevent you from getting alerts about places you research.",
    )
    # Unlike the toggles above, this defaults unchecked - agreement has to be an
    # explicit action, not something a user "leaves on" by not noticing it.
    tos_agreed = forms.BooleanField(
        required=True,
        initial=False,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="I have read and agree to the Terms of Service",
        error_messages={"required": "You need to agree to the Terms of Service to continue."},
    )

    class Meta:
        model = Profile
        fields: list[str] = []

    def save(self, commit: bool = True) -> Profile:
        """Apply the bulk toggles directly onto their underlying "enabled" settings.

        Community's own cascade (pin/visibility forcing) happens in
        ``Profile.save()`` itself, so nothing extra is needed for it here.
        """
        instance = super().save(commit=False)

        memories_enabled = self.cleaned_data["memories_enabled"]
        instance.track_pin_visits = memories_enabled
        instance.track_routes = memories_enabled
        instance.track_geolocation = memories_enabled

        instance.community_enabled = self.cleaned_data["community_enabled"]

        instance.external_apis_enabled = self.cleaned_data["external_apis_enabled"]
        if not instance.external_apis_enabled:
            instance.places_google_enabled = False
            instance.places_nps_enabled = False
            instance.places_wikipedia_enabled = False
            instance.ai_enabled = False

        instance.tos_accepted_at = timezone.now()

        if commit:
            instance.save()
        return instance
