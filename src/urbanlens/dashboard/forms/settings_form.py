"""User settings form - privacy, contact, and style preferences."""

from django import forms

from urbanlens.dashboard.models.direct_messages.meta import MessageRetentionChoice
from urbanlens.dashboard.models.profile.model import (
    DistanceUnit,
    GuidanceLevel,
    MapCenterMode,
    MapViewChoice,
    Profile,
    SyncAliasesDirection,
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
    direct_message_visibility = forms.ChoiceField(
        choices=VisibilityChoice.choices,
        widget=forms.Select(attrs={"class": "settings-select browser-default"}),
        label="Direct Messages",
        help_text="Who can send you direct messages. Anyone you message first can always reply.",
    )
    common_pins_visibility = forms.ChoiceField(
        choices=VisibilityChoice.choices,
        widget=forms.Select(attrs={"class": "settings-select browser-default"}),
        label="Pins in Common",
        help_text="Who can see the specific pins you have in common with them. Requires both of you to allow it.",
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
            "direct_message_visibility",
            "common_pins_visibility",
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


class DirectMessageSettingsForm(forms.ModelForm):
    """Controls direct-message presence privacy, disappearing messages, and friend recommendations."""

    online_status_visibility = forms.ChoiceField(
        choices=VisibilityChoice.choices,
        widget=forms.Select(attrs={"class": "settings-select browser-default"}),
        label="Show Online Status",
        help_text="Who can see when you're online in direct messages.",
    )
    read_receipt_visibility = forms.ChoiceField(
        choices=VisibilityChoice.choices,
        widget=forms.Select(attrs={"class": "settings-select browser-default"}),
        label="Show Read Receipts",
        help_text="Who can see that you've read their direct messages.",
    )
    typing_indicator_visibility = forms.ChoiceField(
        choices=VisibilityChoice.choices,
        widget=forms.Select(attrs={"class": "settings-select browser-default"}),
        label="Show Typing Indicator",
        help_text="Who can see when you're typing a reply.",
    )
    direct_message_delete_after = forms.ChoiceField(
        choices=MessageRetentionChoice.choices,
        widget=forms.Select(attrs={"class": "settings-select browser-default"}),
        label="Delete My Messages After",
        help_text="Messages you send disappear from the recipient's view this long after they've read them. You can always see your own messages.",
    )
    allow_friend_recommendations = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="Allow Friend Recommendations",
        help_text="Let other users recommend you as a friend to people they're messaging.",
    )

    class Meta:
        model = Profile
        fields = [
            "online_status_visibility",
            "read_receipt_visibility",
            "typing_indicator_visibility",
            "direct_message_delete_after",
            "allow_friend_recommendations",
        ]

    def __init__(self, *args, **kwargs):
        """Disable the visibility fields while Community is off, mirroring PrivacySettingsForm."""
        super().__init__(*args, **kwargs)
        if self.instance is not None and not self.instance.community_enabled:
            for name in ("online_status_visibility", "read_receipt_visibility", "typing_indicator_visibility"):
                self.fields[name].disabled = True


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
        label="Guidance",
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
    """AI feature preferences - which label kinds can be auto-assigned on pin creation."""

    ai_enabled = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="Enable AI Features",
        help_text="Turn all AI-assisted features on or off.",
    )
    ai_label_categories = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="Auto-categorize pins",
        help_text="AI automatically suggests and adds a category label when you create a pin.",
    )
    ai_label_tags = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="Auto-tag pins",
        help_text="AI automatically suggests and adds tags when you create a pin.",
    )
    ai_label_statuses = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="Auto-status pins",
        help_text="AI automatically suggests and adds a status label when you create a pin.",
    )

    class Meta:
        model = Profile
        fields = ["ai_enabled", "ai_label_categories", "ai_label_tags", "ai_label_statuses"]


class KeywordTaggingSettingsForm(forms.ModelForm):
    """Keyword-based auto-tagging preferences - independent of the AI settings above.

    Keyword matching is local pattern/substring matching with no external API
    call, so it's available to every user regardless of AI subscription.
    """

    keyword_tagging_enabled = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="Enable Keyword Tagging",
        help_text="Turn all keyword-based auto-tagging on or off.",
    )
    keyword_label_categories = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="Auto-categorize pins",
        help_text="Matching keywords automatically add a category label when you create a pin.",
    )
    keyword_label_tags = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="Auto-tag pins",
        help_text="Matching keywords automatically add tags when you create a pin.",
    )
    keyword_label_statuses = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="Auto-status pins",
        help_text="Matching keywords automatically add a status label when you create a pin.",
    )

    class Meta:
        model = Profile
        fields = ["keyword_tagging_enabled", "keyword_label_categories", "keyword_label_tags", "keyword_label_statuses"]


class HistorySettingsForm(forms.ModelForm):
    """Which visit/location-history categories get saved. Independently adjustable at any time."""

    track_pin_visits = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="Visit History",
        help_text="Log visits to your pins from manual entries, imports, and photo tagging. Also strips GPS data from photos you upload, and stops photo/library scans from suggesting pins based on where you've been.",
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
    generate_photo_keywords = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="Photo Keywords",
        help_text="Automatically generate searchable keywords for photos you upload, so they show up in search. Runs in the background after each upload.",
    )

    class Meta:
        model = Profile
        fields = ["track_pin_visits", "track_routes", "track_geolocation", "generate_photo_keywords"]


class CommunitySettingsForm(forms.ModelForm):
    """Master switch for pin privacy, profile visibility, and friendships."""

    community_enabled = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="Community Features",
        help_text="Enable features that allow you to interact with other users. Community wikis, Trips, and Friend Requests are included in this.",
    )
    show_wiki_cover_photos = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="Show Wiki Cover Photos",
        help_text="Show the community-selected cover photo banner on wiki pages. Turn off if you'd rather not see photos the community has chosen there.",
    )

    class Meta:
        model = Profile
        fields = ["community_enabled", "show_wiki_cover_photos"]


class WikiSyncSettingsForm(forms.ModelForm):
    """Automatic syncing between a pin's private details and its community wiki."""

    sync_rating_to_wiki = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="Rating",
        help_text="When you rate a pin, also count that rating on its community wiki.",
    )
    sync_vulnerability_to_wiki = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="Vulnerability",
        help_text="When you set a pin's vulnerability, also count it on its community wiki.",
    )
    sync_priority_to_wiki = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="Priority",
        help_text="When you set a pin's priority, also count it on its community wiki.",
    )
    sync_danger_to_wiki = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="Danger",
        help_text="When you set a pin's danger, also count it on its community wiki.",
    )
    sync_aliases = forms.ChoiceField(
        choices=SyncAliasesDirection.choices,
        widget=forms.Select(attrs={"class": "settings-select browser-default"}),
        label="Aliases",
        help_text="Automatically copy newly-added alternate names between a pin and its community wiki. Never deletes an alias on either side, and never syncs edits to an existing alias - only new ones.",
    )

    class Meta:
        model = Profile
        fields = ["sync_rating_to_wiki", "sync_vulnerability_to_wiki", "sync_priority_to_wiki", "sync_danger_to_wiki", "sync_aliases"]

    def __init__(self, *args, **kwargs):
        """Disable every field while Community is off, mirroring PrivacySettingsForm."""
        super().__init__(*args, **kwargs)
        if self.instance is not None and not self.instance.community_enabled:
            for field in self.fields.values():
                field.disabled = True


class ExternalApiSettingsForm(forms.ModelForm):
    """Master switch for external API calls made on this profile's behalf."""

    external_apis_enabled = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "settings-toggle-input"}),
        label="External Services",
        help_text="Allow UrbanLens to call external services (weather, geocoding, place data, AI) on your behalf.",
    )
    # Rendered manually via the shared _priority_list.html partial, not the
    # default widget - this field just needs to accept its serialized value.
    name_source_priority = forms.CharField(required=False, widget=forms.HiddenInput())

    class Meta:
        model = Profile
        fields = ["external_apis_enabled", "name_source_priority"]
