"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    model.py                                                                                             *
*        Path:    /dashboard/models/profile/model.py                                                                   *
*        Project: urbanlens                                                                                            *
*        Version: 0.0.2                                                                                                *
*        Created: 2023-12-24                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@urbanlens.org                                                                                 *
*        Copyright (c) 2025 Jess Mann                                                                                  *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2023-12-24     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db.models import (
    CASCADE,
    BooleanField,
    CharField,
    DateField,
    DecimalField,
    ImageField,
    Index,
    IntegerField,
    OneToOneField,
    TextChoices,
    TextField,
)

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.profile.queryset import Manager


class VisibilityChoice(TextChoices):
    """Who can see a particular piece of profile data, or who can perform an action."""

    ANYONE = "anyone", "Anyone"
    FRIENDS = "friends", "Friends Only"
    COMMON_PIN = "common_pin", "Users with a pin in common"
    COMMON_FRIEND = "common_friend", "Users with a friend in common"
    COMMON_TRIP = "common_trip", "Users with a trip in common"
    NO_ONE = "no_one", "No one"


class MapViewChoice(TextChoices):
    STREET = "street", "Street"
    SATELLITE = "satellite", "Satellite"
    TOPOGRAPHIC = "topographic", "Topographic"


class MapCenterMode(TextChoices):
    AUTO = "auto", "Center on my pins"
    GPS = "gps", "Use my current location"
    CUSTOM = "custom", "Custom location"


class Profile(abstract.Model):
    avatar = ImageField(upload_to="avatars/", null=True, blank=True)
    bio = TextField(null=True, blank=True)
    area = CharField(max_length=255, null=True, blank=True)
    birth_date = DateField(null=True, blank=True)
    started_exploring = DateField(null=True, blank=True)

    # Privacy settings
    profile_visibility = CharField(
        max_length=20,
        choices=VisibilityChoice.choices,
        default=VisibilityChoice.ANYONE,
    )
    comment_visibility = CharField(
        max_length=20,
        choices=VisibilityChoice.choices,
        default=VisibilityChoice.ANYONE,
    )
    friend_request_visibility = CharField(
        max_length=20,
        choices=VisibilityChoice.choices,
        default=VisibilityChoice.ANYONE,
    )
    hide_pin_locations_in_trips = BooleanField(
        default=False,
        help_text=(
            "When sharing one of your pins as a trip activity, hide the location "
            "from members who don't already have that pin on their map."
        ),
    )

    # Style preferences
    dark_mode = BooleanField(default=False)
    default_map_view = CharField(
        max_length=20,
        choices=MapViewChoice.choices,
        default=MapViewChoice.SATELLITE,
    )
    # Marker cluster radius in pixels. Null = use the default zoom-based function.
    cluster_radius = IntegerField(null=True, blank=True)
    # When False, pins are always fetched from the server on every map load.
    use_pin_cache = BooleanField(default=True)

    # How the map centers on load.
    map_center_mode = CharField(
        max_length=10,
        choices=MapCenterMode.choices,
        default=MapCenterMode.AUTO,
    )
    # Cached centroid of the user's pins (auto mode). Cleared by post_save signal
    # on new pin; recomputed lazily on the next map load.
    map_center_latitude = DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    map_center_longitude = DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    # User-specified center (custom mode).
    map_custom_latitude = DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    map_custom_longitude = DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    # Default zoom level applied on every map load (all modes).
    map_default_zoom = IntegerField(default=13)

    user = OneToOneField(
        User,
        on_delete=CASCADE,
    )

    objects = Manager()

    @property
    def username(self):
        return self.user.username

    @property
    def email(self):
        return self.user.email

    @property
    def first_name(self):
        return self.user.first_name

    @property
    def last_name(self):
        return self.user.last_name

    @property
    def full_name(self):
        return self.user.get_full_name()

    def compute_map_center(self) -> tuple[float, float] | None:
        """Compute the geographic centroid of all user pins and cache it on the profile.

        Returns:
            (latitude, longitude) tuple, or None if the user has no pins with coordinates.
        """
        from django.db.models import Avg, F
        from django.db.models.functions import Coalesce

        from urbanlens.dashboard.models.pin.model import Pin

        result = Pin.objects.filter(profile=self).aggregate(
            avg_lat=Avg(Coalesce(F("latitude"), F("location__latitude"))),
            avg_lng=Avg(Coalesce(F("longitude"), F("location__longitude"))),
        )
        lat = result.get("avg_lat")
        lng = result.get("avg_lng")
        if lat is None or lng is None:
            return None

        Profile.objects.filter(pk=self.pk).update(
            map_center_latitude=lat,
            map_center_longitude=lng,
        )
        self.map_center_latitude = lat
        self.map_center_longitude = lng
        return float(lat), float(lng)

    def get_map_center(self) -> tuple[float, float] | None:
        """Return the map center coordinates to use as the initial view.

        In GPS mode, returns None — the browser handles centering via geolocation.
        In custom mode, returns the user-stored coordinates.
        In auto mode, returns the cached pin centroid (computing it if needed).

        Returns:
            (latitude, longitude) tuple, or None when the caller should defer to JS.
        """
        if self.map_center_mode == MapCenterMode.GPS:
            return None
        if self.map_center_mode == MapCenterMode.CUSTOM:
            if self.map_custom_latitude is not None and self.map_custom_longitude is not None:
                return float(self.map_custom_latitude), float(self.map_custom_longitude)
            return None
        # AUTO mode
        if self.map_center_latitude is not None and self.map_center_longitude is not None:
            return float(self.map_center_latitude), float(self.map_center_longitude)
        return self.compute_map_center()

    def __str__(self):
        return self.username

    class Meta(abstract.Model.Meta):
        db_table = "dashboard_profiles"

        indexes = [
            Index(fields=["user"]),
        ]
