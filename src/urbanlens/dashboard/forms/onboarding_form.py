"""Form for the first-login /welcome/ page: bulk History/Community/External-APIs toggles."""

from typing import Any

from django import forms
from django.utils import timezone

from urbanlens.dashboard.models.profile.model import Profile

CATEGORY_FIELDS = ("history_enabled", "community_enabled", "external_apis_enabled")


class WelcomeOnboardingForm(forms.ModelForm):
    """One checkbox per feature category, plus a required Terms of Service agreement.

    ``customize_features`` is a UI-only field (not persisted) holding whether the "I want to disable some features"
    accordion is expanded: the page hides the box itself and ``<details data-mirrors-open>`` keeps it equal to the
    accordion's state. Most new users leave it collapsed and continue with everything enabled, since it is purely
    progressive disclosure.
    ``tos_agreed`` is the other exception to the "checked by default" rule - it defaults unchecked,
    since agreement has to be an explicit action rather than something left on by default.
    """

    customize_features = forms.BooleanField(
        required=False,
        initial=False,
        widget=forms.CheckboxInput(attrs={"class": "onboarding-accordion-state", "hidden": True}),
        label="I want to disable some features",
    )
    history_enabled = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="History",
        help_text="Your visit journal, and any location data you choose to upload.",
    )
    community_enabled = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="Community",
        help_text="We support community wikis, trip invitations, and friend requests. Disabling this will turn off those features, making you invisible to other users.",
    )
    external_apis_enabled = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="External Services",
        help_text="We integrate with weather, geocoding, and place data services. Disabling this will turn them off, so you will not see research data unless it was already cached from another user.",
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

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # A re-rendered form keeps a category the user switched off in view, not behind the collapsed accordion.
        self.customize_open = self.is_bound and (bool(self.data.get("customize_features")) or not all(self.data.get(name) for name in CATEGORY_FIELDS))
        if self.customize_open:
            self.fields["customize_features"].widget.attrs["checked"] = True

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
