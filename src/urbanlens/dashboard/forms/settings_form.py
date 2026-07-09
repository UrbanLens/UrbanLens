"""User settings form - privacy, contact, and style preferences."""

from django import forms

from urbanlens.dashboard.models.profile.model import (
    DistanceUnit,
    GuidanceLevel,
    MapCenterMode,
    MapViewChoice,
    Profile,
    ThemeChoice,
    VisibilityChoice,
)


class MarkupDefaultsForm(forms.ModelForm):
    """Default fill/border color and opacity for new pin-detail annotations."""

    markup_fill_color = forms.CharField(
        max_length=20,
        widget=forms.HiddenInput(attrs={"id": "id_markup_fill_color"}),
    )
    markup_fill_opacity = forms.IntegerField(
        min_value=0,
        max_value=100,
        widget=forms.HiddenInput(attrs={"id": "id_markup_fill_opacity"}),
    )
    markup_border_color = forms.CharField(
        max_length=20,
        required=False,
        widget=forms.HiddenInput(attrs={"id": "id_markup_border_color"}),
    )
    markup_border_opacity = forms.IntegerField(
        min_value=0,
        max_value=100,
        widget=forms.HiddenInput(attrs={"id": "id_markup_border_opacity"}),
    )

    class Meta:
        model = Profile
        fields = ["markup_fill_color", "markup_fill_opacity", "markup_border_color", "markup_border_opacity"]

    def clean_markup_fill_color(self):
        color = self.cleaned_data.get("markup_fill_color", "").strip()
        return color or "#e53e3e"

    def clean_markup_border_color(self):
        return (self.cleaned_data.get("markup_border_color") or "").strip()


# "Friends only" is circular for friend requests (they're not friends yet), so exclude it.
_FRIEND_REQUEST_CHOICES = [(k, v) for k, v in VisibilityChoice.choices if k != VisibilityChoice.FRIENDS]


class PrivacySettingsForm(forms.ModelForm):
    """Controls who can see this user's profile, comments, photos, contact info, and friend requests."""

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
        choices=_FRIEND_REQUEST_CHOICES,
        widget=forms.Select(attrs={"class": "settings-select browser-default"}),
        label="Friend Requests",
        help_text="Which users are allowed to send you friend requests.",
    )
    photo_upload_visibility = forms.ChoiceField(
        choices=VisibilityChoice.choices,
        widget=forms.Select(attrs={"class": "settings-select browser-default"}),
        label="Photo Visibility",
        help_text="Who can see the photos you upload to locations.",
    )
    trip_pin_location_visibility = forms.ChoiceField(
        choices=VisibilityChoice.choices,
        widget=forms.Select(attrs={"class": "settings-select browser-default"}),
        label="Trip Pins",
        help_text=("Who can see pins you share to a trip? Other trip members will only see the pin name."),
    )
    viewer_photo_filter = forms.ChoiceField(
        choices=VisibilityChoice.choices,
        widget=forms.Select(attrs={"class": "settings-select browser-default"}),
        label="Show Photos From",
        help_text="Photos you want to see. Other photos will be blurred.",
    )
    contact_visibility = forms.ChoiceField(
        choices=VisibilityChoice.choices,
        widget=forms.Select(attrs={"class": "settings-select browser-default"}),
        label="Contact Visibility",
        help_text="Who can see your contact methods on your profile.",
    )

    class Meta:
        model = Profile
        fields = [
            "profile_visibility",
            "comment_visibility",
            "friend_request_visibility",
            "photo_upload_visibility",
            "viewer_photo_filter",
            "trip_pin_location_visibility",
            "contact_visibility",
        ]

    def __init__(self, *args, **kwargs):
        """Disable every field while Community is off - they're forced to "No one" in Profile.save() anyway.

        ``disabled=True`` both greys the field out in rendering and makes Django
        ignore any posted value for it, so this is also the belt to Profile.save()'s
        suspenders against a tampered POST re-enabling one field at a time.
        """
        super().__init__(*args, **kwargs)
        if self.instance is not None and not self.instance.community_enabled:
            for field in self.fields.values():
                field.disabled = True


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

    def __init__(self, *args, exclude_user_id: int | None = None, **kwargs):
        self._exclude_user_id = exclude_user_id
        super().__init__(*args, **kwargs)

    def clean_email(self) -> str:
        """Reject email addresses already claimed by another account (normalized comparison)."""
        from django.core.exceptions import ValidationError

        from urbanlens.dashboard.services.email_normalization import is_email_taken

        email = self.cleaned_data["email"].strip().lower()
        if is_email_taken(email, exclude_user_id=self._exclude_user_id):
            raise ValidationError("Another account already uses this email address.")
        return email


class StyleSettingsForm(forms.ModelForm):
    """Site-wide appearance - color theme, map dark mode, and in-app help."""

    theme_mode = forms.ChoiceField(
        choices=ThemeChoice.choices,
        widget=forms.RadioSelect(attrs={"class": "settings-radio"}),
        label="Color Theme",
        help_text="System follows your OS preference automatically.",
    )
    map_dark_mode = forms.ChoiceField(
        choices=ThemeChoice.choices,
        widget=forms.RadioSelect(attrs={"class": "settings-radio"}),
        label="Map Dark Mode",
        help_text="When to apply a dark tile layer on the map. System follows your OS preference. Satellite is unaffected.",
    )
    guidance_level = forms.ChoiceField(
        choices=GuidanceLevel.choices,
        widget=forms.RadioSelect(attrs={"class": "settings-radio"}),
        label="In-app Help",
        help_text="Choose how UrbanLens introduces features as you explore.",
    )
    distance_units = forms.ChoiceField(
        choices=DistanceUnit.choices,
        widget=forms.Select(attrs={"class": "settings-select browser-default"}),
        label="Distance Units",
        help_text="Units for distances and travel stats. Defaults to your region.",
    )

    class Meta:
        model = Profile
        fields = ["theme_mode", "map_dark_mode", "guidance_level", "distance_units"]

    def __init__(self, *args, **kwargs):
        """Preselect the region-inferred unit when the user has not chosen one yet."""
        super().__init__(*args, **kwargs)
        if self.instance is not None and self.instance.pk and not self.instance.distance_units:
            self.initial["distance_units"] = self.instance.effective_distance_units


class ContactMethodsForm(forms.ModelForm):
    """Optional contact methods stored on the profile."""

    phone_number = forms.CharField(
        required=False,
        max_length=30,
        widget=forms.TextInput(attrs={"class": "settings-input", "placeholder": "+1 555 000 0000"}),
        label="Phone Number",
    )
    signal_username = forms.CharField(
        required=False,
        max_length=100,
        widget=forms.TextInput(attrs={"class": "settings-input", "placeholder": "username or phone"}),
        label="Signal",
    )
    discord_username = forms.CharField(
        required=False,
        max_length=100,
        widget=forms.TextInput(attrs={"class": "settings-input", "placeholder": "username"}),
        label="Discord",
    )
    whatsapp_number = forms.CharField(
        required=False,
        max_length=30,
        widget=forms.TextInput(attrs={"class": "settings-input", "placeholder": "+1 555 000 0000"}),
        label="WhatsApp",
    )
    telegram_username = forms.CharField(
        required=False,
        max_length=100,
        widget=forms.TextInput(attrs={"class": "settings-input", "placeholder": "@username"}),
        label="Telegram",
    )
    matrix_handle = forms.CharField(
        required=False,
        max_length=200,
        widget=forms.TextInput(attrs={"class": "settings-input", "placeholder": "@user:server.org"}),
        label="Matrix / Element",
    )

    class Meta:
        model = Profile
        fields = [
            "phone_number",
            "signal_username",
            "discord_username",
            "whatsapp_number",
            "telegram_username",
            "matrix_handle",
        ]


class MapDisplayForm(forms.ModelForm):
    """Map display and performance settings."""

    default_map_view = forms.ChoiceField(
        choices=MapViewChoice.choices,
        widget=forms.RadioSelect(attrs={"class": "settings-radio"}),
        label="Default Map View",
        help_text="Which map layer to use by default.",
    )
    cluster_radius = forms.IntegerField(
        required=False,
        min_value=1,
        max_value=500,
        widget=forms.NumberInput(attrs={"class": "settings-input", "placeholder": "Auto (zoom-based)"}),
        label="Cluster Radius",
        help_text="Group pins within this radius. Leave blank for automatic zoom-based grouping.",
    )
    use_pin_cache = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "settings-checkbox"}),
        label="Local Storage",
        help_text=("Cache pins in your browser for instant map loads. Disabling this will make the map feel sluggish."),
    )

    class Meta:
        model = Profile
        fields = ["default_map_view", "cluster_radius", "use_pin_cache"]


class MapCenterForm(forms.ModelForm):
    """Saved map center preference - mode, optional custom coordinates, and default zoom."""

    map_center_mode = forms.ChoiceField(
        choices=MapCenterMode.choices,
        widget=forms.RadioSelect(attrs={"class": "settings-radio"}),
        label="Starting Point",
    )
    map_custom_latitude = forms.DecimalField(
        required=False,
        max_digits=9,
        decimal_places=6,
        widget=forms.HiddenInput(attrs={"id": "id_map_custom_latitude"}),
    )
    map_custom_longitude = forms.DecimalField(
        required=False,
        max_digits=9,
        decimal_places=6,
        widget=forms.HiddenInput(attrs={"id": "id_map_custom_longitude"}),
    )
    map_default_zoom = forms.IntegerField(
        required=False,
        min_value=1,
        max_value=19,
        widget=forms.HiddenInput(attrs={"id": "id_map_default_zoom"}),
    )

    class Meta:
        model = Profile
        fields = ["map_center_mode", "map_custom_latitude", "map_custom_longitude", "map_default_zoom"]

    def clean_map_default_zoom(self):
        zoom = self.cleaned_data.get("map_default_zoom")
        return zoom if zoom is not None else 13

    def save(self, commit: bool = True):
        """Save map center preferences.

        ``map_custom_latitude`` / ``map_custom_longitude`` are only meaningful
        when the mode is CUSTOM.  In GPS or AUTO modes the hidden form fields
        contain whatever the preview map happened to be showing, which must not
        overwrite the user's saved custom location.
        """
        instance = super().save(commit=False)
        if instance.map_center_mode != MapCenterMode.CUSTOM:
            # Restore original custom coordinates from the database.
            original = (type(instance).objects.filter(pk=instance.pk).values("map_custom_latitude", "map_custom_longitude").first()) or {}
            instance.map_custom_latitude = original.get("map_custom_latitude")
            instance.map_custom_longitude = original.get("map_custom_longitude")
        if commit:
            instance.save()
        return instance


class PlacesLayerForm(forms.ModelForm):
    """Which data sources contribute pins to the Places map layer."""

    places_google_enabled = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="Google Historical Landmarks",
        help_text="Show cultural sites, monuments, and historic locations sourced from Google.",
    )
    places_nps_enabled = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="National Park Service",
        help_text="Show US National Parks and monuments within 100 km of the map center.",
    )
    places_wikipedia_enabled = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="Wikipedia",
        help_text="Show Wikipedia-linked places within 5 km of the map center.",
    )

    class Meta:
        model = Profile
        fields = ["places_google_enabled", "places_nps_enabled", "places_wikipedia_enabled"]


class DeleteAccountForm(forms.Form):
    """Confirms an account-deletion request with a password and a typed confirmation phrase."""

    password = forms.CharField(
        label="Password",
        widget=forms.PasswordInput(attrs={"class": "settings-input", "autocomplete": "current-password"}),
    )
    confirm_text = forms.CharField(
        label="Confirmation",
        widget=forms.TextInput(attrs={"class": "settings-input", "autocomplete": "off", "spellcheck": "false"}),
    )

    def __init__(self, *args, user=None, **kwargs):
        self._user = user
        super().__init__(*args, **kwargs)

    def clean_password(self) -> str:
        password = self.cleaned_data["password"]
        if not self._user or not self._user.check_password(password):
            raise forms.ValidationError("Incorrect password.")
        return password

    def clean_confirm_text(self) -> str:
        expected = f"delete {self._user.username}" if self._user else ""
        confirm_text = self.cleaned_data["confirm_text"].strip()
        if confirm_text.lower() != expected.lower():
            raise forms.ValidationError(f'Type "{expected}" exactly to confirm.')
        return confirm_text


class AISettingsForm(forms.ModelForm):
    """AI feature preferences - which badge kinds can be auto-assigned on pin creation."""

    ai_enabled = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="Enable AI Features",
        help_text="Turn all AI-assisted features on or off.",
    )
    ai_badge_categories = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="Auto-categorize pins",
        help_text="AI automatically suggests and adds a category badge when you create a pin.",
    )
    ai_badge_tags = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="Auto-tag pins",
        help_text="AI automatically suggests and adds tags when you create a pin.",
    )
    ai_badge_statuses = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="Auto-status pins",
        help_text="AI automatically suggests and adds a status badge when you create a pin.",
    )

    class Meta:
        model = Profile
        fields = ["ai_enabled", "ai_badge_categories", "ai_badge_tags", "ai_badge_statuses"]


class MemoriesSettingsForm(forms.ModelForm):
    """Which visit/location-history categories get saved. Independently adjustable at any time."""

    track_pin_visits = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="Visit History",
        help_text="Log visits to your pins from manual entries, imports, and photo tagging.",
    )
    track_routes = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="GPS Routes",
        help_text="Save imported GPS routes/tracks.",
    )
    track_geolocation = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="Live Location",
        help_text="Record visits from your live device location.",
    )

    class Meta:
        model = Profile
        fields = ["track_pin_visits", "track_routes", "track_geolocation"]


class CommunitySettingsForm(forms.ModelForm):
    """Master switch for pin privacy, profile visibility, and friendships."""

    community_enabled = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="Community Features",
        help_text="Allow other users to see your pins, view your profile, and send/receive friend requests.",
    )

    class Meta:
        model = Profile
        fields = ["community_enabled"]


class ExternalApiSettingsForm(forms.ModelForm):
    """Master switch for external API calls made on this profile's behalf."""

    external_apis_enabled = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="External Services",
        help_text="Allow UrbanLens to call external services (weather, geocoding, place data, AI) on your behalf.",
    )

    class Meta:
        model = Profile
        fields = ["external_apis_enabled"]
