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
        label="Who Can Send Friend Requests",
        help_text="Control which users are allowed to send you friend requests.",
    )
    hide_pin_locations_in_trips = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "settings-checkbox"}),
        label="Hide My Pin Locations in Trips",
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
    """Display preferences saved on the Profile."""

    dark_mode = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "settings-checkbox"}),
        label="Dark Mode",
        help_text="Use a dark color scheme across the app.",
    )
    default_map_view = forms.ChoiceField(
        choices=MapViewChoice.choices,
        widget=forms.RadioSelect(attrs={"class": "settings-radio"}),
        label="Default Map View",
        help_text="Which map layer to use by default on location pages.",
    )
    cluster_radius = forms.IntegerField(
        required=False,
        min_value=1,
        max_value=500,
        widget=forms.NumberInput(attrs={"class": "settings-input", "placeholder": "Auto (zoom-based)"}),
        label="Cluster Radius",
        help_text="Pixels within which nearby pins are grouped into a cluster. Leave blank to use the automatic zoom-based value.",
    )
    use_pin_cache = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "settings-checkbox"}),
        label="Cache pins in browser storage",
        help_text=(
            "Stores your pins in your browser so the map loads instantly on repeat visits. "
            "Disabling this means pins must be re-fetched from the server every time you open the map, "
            "which will noticeably slow down load times — especially if you have many pins."
        ),
    )

    class Meta:
        model = Profile
        fields = ["dark_mode", "default_map_view", "cluster_radius", "use_pin_cache"]


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
