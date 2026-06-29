"""User settings form - privacy, contact, and style preferences."""

from django import forms

from urbanlens.dashboard.models.profile.model import (
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
        help_text=(
            "Who can see pins you share to a trip? Other trip members will only see the pin name."
        ),
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

    class Meta:
        model = Profile
        fields = ["theme_mode", "map_dark_mode", "guidance_level"]


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
            original = (
                type(instance)
                .objects.filter(pk=instance.pk)
                .values("map_custom_latitude", "map_custom_longitude")
                .first()
            ) or {}
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


class AISettingsForm(forms.ModelForm):
    """AI feature preferences - which badge kinds can be auto-assigned on pin creation."""

    ai_enabled = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="Enable AI Features",
        help_text="Turn all AI-assisted features on or off for your account.",
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
