"""Form for the first-login /welcome/ page: bulk History/Community/External-APIs toggles."""

from django import forms
from django.utils import timezone

from urbanlens.dashboard.models.profile.model import Profile


class WelcomeOnboardingForm(forms.ModelForm):
    """One checkbox per feature category, plus a required Terms of Service agreement.

    The three category checkboxes are each checked (fully-featured) by default; unchecking one
    disables that category. Copy for each is written feature-first ("what this covers"), ending
    with an explicit "Disabling this will turn off X, Y, Z" sentence, so it's unambiguous that the
    switch being *on* means the features are active. ``customize_features`` is a UI-only toggle
    (not persisted) that reveals those three cards - most new users can leave it off and continue
    with everything enabled, since the toggle is purely progressive disclosure. ``tos_agreed`` is
    the other exception to the "checked by default" rule - it defaults unchecked, since agreement
    has to be an explicit action rather than something left on by default. None of the fields are
    bound to ``Profile`` via ``Meta.fields`` - the three category fields are bulk-applied to one or
    more underlying settings on ``save()``. The category settings remain independently adjustable
    afterward from the settings page; this form only offers the bulk "leave on/turn off" choice for
    a quick first impression.
    """

    customize_features = forms.BooleanField(
        required=False,
        initial=False,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="Customize which features are enabled",
    )
    history_enabled = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="History",
        help_text="Your visit journal, GPS route imports, live location tracking, and GPS data kept on uploaded photos. Disabling this will turn off your visit journal, strip GPS data from photos you upload, and stop the site from recording your live location or importing GPS routes. Your Maps and Sharing pages aren't affected.",
    )
    community_enabled = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="Community",
        help_text="Community wikis, trip invitations, and friend requests. Disabling this will turn off community wikis, trip invitations, and friend requests, making you invisible to other users.",
    )
    external_apis_enabled = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="External Services",
        help_text="Weather, geocoding, place data, web searches, and AI research about locations. Disabling this will turn off all of the above, so no research data will be displayed unless it was already cached from another user's request.",
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

        history_enabled = self.cleaned_data["history_enabled"]
        instance.track_pin_visits = history_enabled
        instance.track_routes = history_enabled
        instance.track_geolocation = history_enabled

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
