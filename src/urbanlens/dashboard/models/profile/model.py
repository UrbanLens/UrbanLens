from __future__ import annotations

import math
from typing import TYPE_CHECKING

from django.contrib.auth.models import User
from django.db import models
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
    SlugField,
    TextChoices,
    TextField,
)

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.profile.meta import GuidanceLevel, MapCenterMode, MapViewChoice, ThemeChoice, VisibilityChoice
from urbanlens.dashboard.models.profile.queryset import ProfileManager

if TYPE_CHECKING:
    from django.db.models import Manager as DjangoManager

    from urbanlens.dashboard.models.badges.queryset import BadgeManager
    from urbanlens.dashboard.models.markup.model import PinMarkup
    from urbanlens.dashboard.models.notifications.model import NotificationLog
    from urbanlens.dashboard.models.trips import Trip, TripActivity, TripMembership

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


class Profile(abstract.HasSlug):
    # Global uniqueness with a shorter cap to fit within username length limits.
    slug = SlugField(max_length=150, null=True, blank=True, unique=True)

    avatar = ImageField(upload_to="avatars/", null=True, blank=True)
    profile_setup_complete = BooleanField(default=True)
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
    photo_upload_visibility = CharField(
        max_length=20,
        choices=VisibilityChoice.choices,
        default=VisibilityChoice.ANYONE,
        help_text="Who can see the photos you upload to locations.",
    )
    viewer_photo_filter = CharField(
        max_length=20,
        choices=VisibilityChoice.choices,
        default=VisibilityChoice.ANYONE,
        help_text="Whose photos you want to see. Photos from users outside this setting will be blurred.",
    )
    trip_pin_location_visibility = CharField(
        max_length=20,
        choices=VisibilityChoice.choices,
        default=VisibilityChoice.ANYONE,
        help_text=("When you share one of your pins as a trip activity, who can see the actual location? Members outside this setting will only see the pin name."),
    )

    # Cached normalized form of user.email (kept in sync by a User post_save
    # signal) so email-match lookups (friend invites, dup checks, login) are a
    # single indexed query instead of a full-table Python scan.
    primary_email_normalized = CharField(max_length=254, blank=True, default="", db_index=True)

    # Contact information and its visibility
    phone_number = CharField(max_length=30, blank=True, default="")
    signal_username = CharField(max_length=100, blank=True, default="")
    discord_username = CharField(max_length=100, blank=True, default="")
    whatsapp_number = CharField(max_length=30, blank=True, default="")
    telegram_username = CharField(max_length=100, blank=True, default="")
    matrix_handle = CharField(max_length=200, blank=True, default="")
    contact_visibility = CharField(
        max_length=20,
        choices=VisibilityChoice.choices,
        default=VisibilityChoice.FRIENDS,
        help_text="Who can see your contact methods (phone, Signal, Discord, etc.).",
    )

    # Style preferences
    theme_mode = CharField(
        max_length=10,
        choices=ThemeChoice.choices,
        default=ThemeChoice.SYSTEM,
    )
    guidance_level = CharField(
        max_length=10,
        choices=GuidanceLevel.choices,
        default=GuidanceLevel.ALL,
        help_text="Whether to show feature walkthroughs, and hover hints.",
    )
    map_dark_mode = CharField(
        max_length=10,
        choices=ThemeChoice.choices,
        default=ThemeChoice.LIGHT,
        help_text="When to apply a dark tile layer on the map. System follows your OS preference. Satellite is always unaffected.",
    )
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

    # Remembered position (remember mode): last pan/zoom saved by JS.
    remembered_map_lat = DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    remembered_map_lng = DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    remembered_map_zoom = IntegerField(null=True, blank=True)

    # Default styling for new pin-detail annotations (lines, arrows, shapes, text).
    # Opacity is stored as an integer 0-100 (percent).
    markup_fill_color = CharField(max_length=20, default="#e53e3e")
    markup_fill_opacity = IntegerField(default=87)
    # Empty string = no border (transparent). Hex string = explicit border color.
    markup_border_color = CharField(max_length=20, blank=True, default="")
    markup_border_opacity = IntegerField(default=100)

    # AI feature preferences (only relevant when the user has an AI subscription).
    ai_enabled = BooleanField(default=True, help_text="Allow AI features on your account.")
    ai_badge_tags = BooleanField(default=True, help_text="AI can automatically suggest and add tags when a pin is created.")
    ai_badge_categories = BooleanField(default=True, help_text="AI can automatically suggest and add categories when a pin is created.")
    ai_badge_statuses = BooleanField(default=True, help_text="AI can automatically suggest and add statuses when a pin is created.")

    # Places layer source preferences (only relevant when the user has the PLACES feature).
    places_google_enabled = BooleanField(default=True, help_text="Show Google historical landmarks in the Places layer.")
    places_nps_enabled = BooleanField(default=True, help_text="Show National Park Service locations in the Places layer.")
    places_wikipedia_enabled = BooleanField(default=True, help_text="Show Wikipedia-linked places in the Places layer.")

    user = OneToOneField(
        User,
        on_delete=CASCADE,
    )

    objects = ProfileManager()

    if TYPE_CHECKING:
        user_id: int
        trip_activities_added: DjangoManager[TripActivity]
        created_trips: DjangoManager[Trip]
        trips: DjangoManager[Trip]
        custom_tags: BadgeManager
        trip_memberships: DjangoManager[TripMembership]
        notifications: DjangoManager[NotificationLog]
        triggered_notifications: DjangoManager[NotificationLog]
        markup_items: DjangoManager[PinMarkup]

    @property
    def show_onboarding_tips(self) -> bool:
        """Whether contextual walkthrough cards should be shown."""
        return self.guidance_level == GuidanceLevel.ALL

    @property
    def show_hover_tooltips(self) -> bool:
        """Whether button hover/focus hints should be shown."""
        return self.guidance_level != GuidanceLevel.NONE

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

    def _slugify_base(self) -> str:
        return self.user.username or "user"

    def save(self, *args, **kwargs) -> None:
        """Auto-generate a unique slug from the username if not already set."""
        self.ensure_slug()
        super().save(*args, **kwargs)

    def compute_map_center(self) -> tuple[float, float] | None:
        """Find the densest geographic cluster of pins and return its centroid.

        A naive average breaks when the user has pins on multiple continents -
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
            key=lambda i: sum(1 for other in pts if _haversine_km(pts[i], other) <= _CLUSTER_RADIUS_KM),
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

        In GPS mode, returns None - the browser handles centering via geolocation.
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
        if self.map_center_mode == MapCenterMode.REMEMBER:
            if self.remembered_map_lat is not None and self.remembered_map_lng is not None:
                return float(self.remembered_map_lat), float(self.remembered_map_lng)
            return None
        # AUTO mode
        if self.map_center_latitude is not None and self.map_center_longitude is not None:
            return float(self.map_center_latitude), float(self.map_center_longitude)
        return self.compute_map_center()

    def get_map_center_template_context(self) -> dict[str, float | str | None]:
        """Return template variables for client-side map centering.

        Mirrors the main map page: server coordinates when the profile mode
        supplies them, plus a pin-cluster fallback for GPS mode when the browser
        denies geolocation.

        Returns:
            Dict with ``map_center_lat``, ``map_center_lng``, ``map_center_mode``,
            ``gps_fallback_lat``, and ``gps_fallback_lng`` keys.
        """
        map_center = self.get_map_center()
        gps_fallback: tuple[float, float] | None = None
        if self.map_center_mode == MapCenterMode.GPS:
            if self.map_center_latitude is not None and self.map_center_longitude is not None:
                gps_fallback = (float(self.map_center_latitude), float(self.map_center_longitude))
            else:
                gps_fallback = self.compute_map_center()
        return {
            "map_center_lat": map_center[0] if map_center else None,
            "map_center_lng": map_center[1] if map_center else None,
            "map_center_mode": self.map_center_mode,
            "gps_fallback_lat": gps_fallback[0] if gps_fallback else None,
            "gps_fallback_lng": gps_fallback[1] if gps_fallback else None,
        }

    def can_view_photos_from(self, uploader: Profile) -> bool:
        """Return True if this profile is allowed to see photos uploaded by ``uploader``.

        Both directions are enforced:
        - The uploader's ``photo_upload_visibility`` must permit this viewer.
        - This viewer's ``viewer_photo_filter`` must permit photos from the uploader.
        """
        if self == uploader:
            return True

        def _check(visibility: str, subject: Profile, other: Profile) -> bool:
            """Return True if ``subject``'s ``visibility`` setting permits ``other``."""
            if visibility == VisibilityChoice.ANYONE:
                return True
            if visibility == VisibilityChoice.NO_ONE:
                return False
            if visibility == VisibilityChoice.FRIENDS:
                from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus

                return Friendship.objects.filter(
                    models.Q(from_profile=subject, to_profile=other) | models.Q(from_profile=other, to_profile=subject),
                    status=FriendshipStatus.ACCEPTED,
                ).exists()
            if visibility == VisibilityChoice.COMMON_PIN:
                from urbanlens.dashboard.models.pin.model import Pin

                my_locs = set(
                    Pin.objects.filter(profile=subject, location__isnull=False).values_list("location_id", flat=True),
                )
                their_locs = set(
                    Pin.objects.filter(profile=other, location__isnull=False).values_list("location_id", flat=True),
                )
                return bool(my_locs & their_locs)
            if visibility == VisibilityChoice.COMMON_FRIEND:
                from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus

                accepted = FriendshipStatus.ACCEPTED
                my_friends = set(
                    Friendship.objects.filter(from_profile=subject, status=accepted).values_list(
                        "to_profile_id",
                        flat=True,
                    ),
                ) | set(
                    Friendship.objects.filter(to_profile=subject, status=accepted).values_list(
                        "from_profile_id",
                        flat=True,
                    ),
                )
                their_friends = set(
                    Friendship.objects.filter(from_profile=other, status=accepted).values_list(
                        "to_profile_id",
                        flat=True,
                    ),
                ) | set(
                    Friendship.objects.filter(to_profile=other, status=accepted).values_list(
                        "from_profile_id",
                        flat=True,
                    ),
                )
                return bool(my_friends & their_friends)
            if visibility == VisibilityChoice.COMMON_TRIP:
                from urbanlens.dashboard.models.trips.model import TripMembership

                my_trips = set(TripMembership.objects.filter(profile=subject).values_list("trip_id", flat=True))
                their_trips = set(TripMembership.objects.filter(profile=other).values_list("trip_id", flat=True))
                return bool(my_trips & their_trips)
            return False

        # Uploader must allow this viewer.
        if not _check(uploader.photo_upload_visibility, uploader, self):
            return False
        # This viewer's filter must allow the uploader.
        return _check(self.viewer_photo_filter, self, uploader)

    def can_view_contact_info(self, viewer: Profile | None) -> bool:
        """Return True if viewer may see this profile's contact methods.

        Args:
            viewer: The profile requesting access, or None for anonymous visitors.

        Returns:
            True when the viewer passes the contact_visibility setting.
        """
        if viewer is not None and self.pk == viewer.pk:
            return True

        def _ck(visibility: str, subject: Profile, other: Profile) -> bool:
            if visibility == VisibilityChoice.ANYONE:
                return True
            if visibility == VisibilityChoice.NO_ONE:
                return False
            if visibility == VisibilityChoice.FRIENDS:
                from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus

                return Friendship.objects.filter(
                    models.Q(from_profile=subject, to_profile=other) | models.Q(from_profile=other, to_profile=subject),
                    status=FriendshipStatus.ACCEPTED,
                ).exists()
            if visibility == VisibilityChoice.COMMON_PIN:
                from urbanlens.dashboard.models.pin.model import Pin

                my_locs = set(Pin.objects.filter(profile=subject, location__isnull=False).values_list("location_id", flat=True))
                their_locs = set(Pin.objects.filter(profile=other, location__isnull=False).values_list("location_id", flat=True))
                return bool(my_locs & their_locs)
            if visibility == VisibilityChoice.COMMON_FRIEND:
                from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus

                accepted = FriendshipStatus.ACCEPTED
                my_friends = set(Friendship.objects.filter(from_profile=subject, status=accepted).values_list("to_profile_id", flat=True)) | set(Friendship.objects.filter(to_profile=subject, status=accepted).values_list("from_profile_id", flat=True))
                their_friends = set(Friendship.objects.filter(from_profile=other, status=accepted).values_list("to_profile_id", flat=True)) | set(Friendship.objects.filter(to_profile=other, status=accepted).values_list("from_profile_id", flat=True))
                return bool(my_friends & their_friends)
            if visibility == VisibilityChoice.COMMON_TRIP:
                from urbanlens.dashboard.models.trips.model import TripMembership

                my_trips = set(TripMembership.objects.filter(profile=subject).values_list("trip_id", flat=True))
                their_trips = set(TripMembership.objects.filter(profile=other).values_list("trip_id", flat=True))
                return bool(my_trips & their_trips)
            return False

        if self.contact_visibility == VisibilityChoice.ANYONE:
            return True
        if viewer is None:
            return False
        return _ck(self.contact_visibility, self, viewer)

    def __str__(self):
        return self.username

    class Meta(abstract.HasSlug.Meta):
        db_table = "dashboard_profiles"

        indexes = [
            Index(fields=["user"], name="idxdb_profile_user"),
        ]
