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

import math

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

# Pins within this distance are considered part of the same cluster.
# 1 000 km groups intra-continental pins together while keeping intercontinental
# collections (e.g. US east coast vs Europe, ~5 600 km) in separate clusters.
_CLUSTER_RADIUS_KM = 1_000.0


def _haversine_km(p1: tuple[float, float], p2: tuple[float, float]) -> float:
    """Great-circle distance in kilometres between two (lat, lng) points."""
    lat1, lng1 = math.radians(p1[0]), math.radians(p1[1])
    lat2, lng2 = math.radians(p2[0]), math.radians(p2[1])
    dlat, dlng = lat2 - lat1, lng2 - lng1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 6_371.0 * 2 * math.asin(math.sqrt(a))


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
        default=MapCenterMode.GPS,
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
        """Find the densest geographic cluster of pins and return its centroid.

        A naive average breaks when the user has pins on multiple continents —
        the centre point ends up in the ocean between them.  Instead we find the
        "seed" point with the most neighbours within _CLUSTER_RADIUS_KM, then
        return the centroid of those neighbours.  For a single tight collection
        this equals the regular centroid; for intercontinental spreads the
        largest regional cluster wins.

        Returns:
            (latitude, longitude) as floats, or None if the user has no pins
            with resolvable coordinates.
        """
        from django.db.models import F
        from django.db.models.functions import Coalesce

        from urbanlens.dashboard.models.pin.model import Pin

        rows = list(
            Pin.objects.filter(profile=self)
            .annotate(
                eff_lat=Coalesce(F("latitude"), F("location__latitude")),
                eff_lng=Coalesce(F("longitude"), F("location__longitude")),
            )
            .filter(eff_lat__isnull=False, eff_lng__isnull=False)
            .values_list("eff_lat", "eff_lng"),
        )
        if not rows:
            return None

        pts = [(float(lat), float(lng)) for lat, lng in rows]

        # For each point count how many other points fall within the cluster radius.
        # The point with the highest count is the cluster seed.
        best_idx = max(
            range(len(pts)),
            key=lambda i: sum(
                1 for other in pts
                if _haversine_km(pts[i], other) <= _CLUSTER_RADIUS_KM
            ),
        )

        seed = pts[best_idx]
        cluster = [p for p in pts if _haversine_km(seed, p) <= _CLUSTER_RADIUS_KM]
        avg_lat = sum(p[0] for p in cluster) / len(cluster)
        avg_lng = sum(p[1] for p in cluster) / len(cluster)

        Profile.objects.filter(pk=self.pk).update(
            map_center_latitude=avg_lat,
            map_center_longitude=avg_lng,
        )
        self.map_center_latitude = avg_lat
        self.map_center_longitude = avg_lng
        return avg_lat, avg_lng

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
