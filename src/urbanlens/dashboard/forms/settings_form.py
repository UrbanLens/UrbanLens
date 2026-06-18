"""User settings form - privacy, contact, and style preferences."""

from django import forms

from urbanlens.dashboard.models.profile.model import MapCenterMode, MapViewChoice, Profile, VisibilityChoice

# "Friends only" is circular for friend requests (they're not friends yet), so exclude it.
_FRIEND_REQUEST_CHOICES = [(k, v) for k, v in VisibilityChoice.choices if k != VisibilityChoice.FRIENDS]


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
        choices=_FRIEND_REQUEST_CHOICES,
        widget=forms.Select(attrs={"class": "settings-select browser-default"}),
        label="Friend Requests",
        help_text="Which users are allowed to send you friend requests.",
    )
    hide_pin_locations_in_trips = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "settings-checkbox"}),
        label="Hide My Pins in Trips",
        help_text=(
            "When you share one of your pins as a trip activity, hide the location "
            "from trip members who don't already have that pin on their own map."
        ),
    )

    class Meta:
        model = Profile
        fields = ["profile_visibility", "comment_visibility", "friend_request_visibility", "hide_pin_locations_in_trips"]


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
    """Site-wide appearance — color theme only."""

    dark_mode = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "settings-checkbox"}),
        label="Dark Mode",
        help_text="Use a dark color scheme across the app.",
    )

    class Meta:
        model = Profile
        fields = ["dark_mode"]


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
        help_text="Pixels within which nearby pins are grouped. Leave blank for automatic zoom-based grouping.",
    )
    use_pin_cache = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "settings-checkbox"}),
        label="Local Storage",
        help_text=(
            "Caches your pins in the browser for instant map loads on return visits. "
            "Disabling this re-fetches all pins from the server on every load."
        ),
    )

    class Meta:
        model = Profile
        fields = ["default_map_view", "cluster_radius", "use_pin_cache"]


class MapCenterForm(forms.ModelForm):
    """Saved map center preference — mode, optional custom coordinates, and default zoom."""

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
                type(instance).objects
                .filter(pk=instance.pk)
                .values("map_custom_latitude", "map_custom_longitude")
                .first()
            ) or {}
            instance.map_custom_latitude = original.get("map_custom_latitude")
            instance.map_custom_longitude = original.get("map_custom_longitude")
        if commit:
            instance.save()
        return instance
