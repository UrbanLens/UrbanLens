from __future__ import annotations

import datetime
import math
from typing import TYPE_CHECKING

from django.contrib.auth.models import User
from django.core.validators import MaxLengthValidator
from django.db import models
from django.db.models import (
    CASCADE,
    BooleanField,
    CharField,
    DateField,
    DateTimeField,
    DecimalField,
    ImageField,
    Index,
    IntegerField,
    OneToOneField,
    SlugField,
    TextChoices,
    TextField,
)
from django.utils import timezone

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.direct_messages.meta import MessageRetentionChoice
from urbanlens.dashboard.models.profile.meta import DistanceUnit, GuidanceLevel, MapCenterMode, MapViewChoice, ThemeChoice, VisibilityChoice
from urbanlens.dashboard.models.profile.queryset import ProfileManager
from urbanlens.dashboard.services.text_limits import MAX_PROFILE_BIO_LENGTH

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

# How long a soft-deleted account stays recoverable before the hard delete runs.
ACCOUNT_DELETION_GRACE_PERIOD = datetime.timedelta(days=7)
# How long before the hard delete the "1 day left" reminder goes out.
ACCOUNT_DELETION_REMINDER_LEAD = datetime.timedelta(days=1)

# Visibility fields forced to VisibilityChoice.NO_ONE while community_enabled is
# False. Re-enabling community makes them editable again; their values are not
# restored to whatever they were before - they stay at NO_ONE until re-chosen.
_COMMUNITY_GATED_VISIBILITY_FIELDS = (
    "profile_visibility",
    "comment_visibility",
    "friend_request_visibility",
    "photo_upload_visibility",
    "viewer_photo_filter",
    "trip_pin_location_visibility",
    "contact_visibility",
    "direct_message_visibility",
    "online_status_visibility",
    "read_receipt_visibility",
    "typing_indicator_visibility",
)


def _haversine_km(p1: tuple[float, float], p2: tuple[float, float]) -> float:
    """Great-circle distance in kilometres between two (lat, lng) points."""
    lat1, lng1 = math.radians(p1[0]), math.radians(p1[1])
    lat2, lng2 = math.radians(p2[0]), math.radians(p2[1])
    dlat, dlng = lat2 - lat1, lng2 - lng1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 6_371.0 * 2 * math.asin(math.sqrt(a))


# Rough lat/lng bounding boxes for the regions that use miles for everyday road
# distances. Used only to pick a sensible *default* distance unit when the user
# has not chosen one explicitly; a false negative just falls back to kilometres.
# Each entry is (min_lat, max_lat, min_lng, max_lng).
_MILES_REGION_BBOXES: tuple[tuple[float, float, float, float], ...] = (
    (24.0, 50.0, -125.0, -66.0),  # Contiguous United States
    (51.0, 72.0, -170.0, -129.0),  # Alaska
    (18.0, 23.0, -161.0, -154.0),  # Hawaii
    (49.5, 61.0, -8.7, 2.0),  # United Kingdom
    (4.0, 9.0, -12.0, -7.0),  # Liberia
    (9.0, 29.0, 92.0, 102.0),  # Myanmar
)


def _units_for_point(lat: float, lng: float) -> str:
    """Return the default distance unit for a geographic point.

    Points inside a miles-using region resolve to miles; everything else
    (and any point that cannot be classified) defaults to kilometres.
    """
    for min_lat, max_lat, min_lng, max_lng in _MILES_REGION_BBOXES:
        if min_lat <= lat <= max_lat and min_lng <= lng <= max_lng:
            return DistanceUnit.MILES
    return DistanceUnit.KILOMETERS


class Profile(abstract.PublicDashboardModel):
    # Global uniqueness with a shorter cap to fit within username length limits.
    slug = SlugField(max_length=150, null=True, blank=True, unique=True)

    avatar = ImageField(upload_to="avatars/", null=True, blank=True)
    profile_setup_complete = BooleanField(default=True)
    # Default False so every newly-created profile shows /welcome/ once with no
    # signup-path race; the migration that adds this field backfills existing
    # rows to True so pre-existing accounts never see it.
    welcome_onboarding_complete = BooleanField(default=False)
    # Set the moment the user checks the "I agree" box on /welcome/. Null means
    # never agreed - existing accounts are backfilled to their profile creation
    # date (accepting terms is implied by having used the site already).
    tos_accepted_at = DateTimeField(null=True, blank=True)
    bio = TextField(null=True, blank=True, max_length=MAX_PROFILE_BIO_LENGTH, validators=[MaxLengthValidator(MAX_PROFILE_BIO_LENGTH)])
    area = CharField(max_length=255, null=True, blank=True)
    birth_date = DateField(null=True, blank=True)
    started_exploring = DateField(null=True, blank=True)

    # Privacy settings
    profile_visibility = CharField(
        max_length=20,
        choices=VisibilityChoice.choices,
        default=VisibilityChoice.ANYTHING_IN_COMMON,
    )
    comment_visibility = CharField(
        max_length=20,
        choices=VisibilityChoice.choices,
        default=VisibilityChoice.ANYTHING_IN_COMMON,
    )
    friend_request_visibility = CharField(
        max_length=20,
        choices=VisibilityChoice.choices,
        default=VisibilityChoice.ANYTHING_IN_COMMON,
    )
    photo_upload_visibility = CharField(
        max_length=20,
        choices=VisibilityChoice.choices,
        default=VisibilityChoice.ANYTHING_IN_COMMON,
        help_text="Who can see the photos you upload to locations.",
    )
    viewer_photo_filter = CharField(
        max_length=20,
        choices=VisibilityChoice.choices,
        default=VisibilityChoice.ANYTHING_IN_COMMON,
        help_text="Whose photos you want to see. Photos from users outside this setting will be blurred.",
    )
    trip_pin_location_visibility = CharField(
        max_length=20,
        choices=VisibilityChoice.choices,
        default=VisibilityChoice.ANYTHING_IN_COMMON,
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
    direct_message_visibility = CharField(
        max_length=20,
        choices=VisibilityChoice.choices,
        default=VisibilityChoice.ANYTHING_IN_COMMON,
        help_text="Who can send you direct messages.",
    )
    online_status_visibility = CharField(
        max_length=20,
        choices=VisibilityChoice.choices,
        default=VisibilityChoice.FRIENDS,
        help_text="Who can see when you're online in direct messages.",
    )
    read_receipt_visibility = CharField(
        max_length=20,
        choices=VisibilityChoice.choices,
        default=VisibilityChoice.FRIENDS,
        help_text="Who can see that you've read their direct messages.",
    )
    typing_indicator_visibility = CharField(
        max_length=20,
        choices=VisibilityChoice.choices,
        default=VisibilityChoice.FRIENDS,
        help_text="Who can see when you're typing a direct message reply.",
    )
    direct_message_delete_after = CharField(
        max_length=20,
        choices=MessageRetentionChoice.choices,
        default=MessageRetentionChoice.NEVER,
        help_text="Messages you send disappear from the recipient's view this long after they've read them. You can always see your own messages.",
    )
    allow_friend_recommendations = BooleanField(
        default=True,
        help_text="Let other users recommend you as a friend to people they're messaging.",
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
    # Preferred unit for displayed distances. Null means "not chosen yet" - the
    # effective unit is then inferred from the user's location (see
    # effective_distance_units), defaulting to kilometres.
    distance_units = CharField(
        max_length=4,
        choices=DistanceUnit.choices,
        null=True,
        blank=True,
        help_text="Unit used for distances and travel stats. Defaults to your region.",
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

    # Voluntary downscale cap (longest edge, px) for future photo uploads.
    # Null means "use whatever the site policy entitles me to". A value here can
    # only tighten the site policy (the effective cap is the smaller of the two),
    # letting users trade image resolution for more photos within their quota.
    image_downscale_max_dimension = IntegerField(null=True, blank=True)

    # Places layer source preferences (only relevant when the user has the PLACES feature).
    places_google_enabled = BooleanField(default=True, help_text="Show Google historical landmarks in the Places layer.")
    places_nps_enabled = BooleanField(default=True, help_text="Show National Park Service locations in the Places layer.")
    places_wikipedia_enabled = BooleanField(default=True, help_text="Show Wikipedia-linked places in the Places layer.")

    # Memories preferences. Each independently controls whether a category of
    # visit/location history is ever saved - including from explicit user actions
    # like GPX/Takeout imports, not just passive/background tracking.
    track_pin_visits = BooleanField(default=True, help_text="Log visits to your pins from journal entries, imports, and photo tagging.")
    track_routes = BooleanField(default=True, help_text="Save imported GPS routes/tracks.")
    track_geolocation = BooleanField(default=True, help_text="Record visits from your live device location.")

    # When False: your pins are forced private, your profile and privacy
    # settings are locked to the most restrictive option, and you cannot send
    # or receive friend requests. Enforced in Pin.save()/Profile.save().
    community_enabled = BooleanField(default=True, help_text="Enable features that allow you to interact with other users. Community wikis, Trips, and Friend Requests are included in this.")

    # Master switch for all external API calls made on your behalf (weather,
    # geocoding, place data, AI, etc). Individual services also have their own
    # toggles below/elsewhere that remain independently adjustable.
    external_apis_enabled = BooleanField(default=True, help_text="Allow external services (weather, geocoding, place data, AI) to retrieve anonymized research data for you.")

    # Set when the user requests account deletion; cleared on cancel/undo.
    # A non-null value means the account is scheduled for hard deletion at
    # deletion_requested_at + ACCOUNT_DELETION_GRACE_PERIOD.
    deletion_requested_at = DateTimeField(null=True, blank=True, db_index=True)
    # Idempotency guard so the "1 day left" reminder is sent at most once per
    # deletion request; cleared alongside deletion_requested_at on cancel.
    deletion_reminder_sent_at = DateTimeField(null=True, blank=True)

    user = OneToOneField(
        User,
        on_delete=CASCADE,
    )

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

    objects = ProfileManager()

    def save(self, *args, **kwargs) -> None:
        """Save the profile, forcing visibility settings to their most restrictive value while Community is off.

        This single enforcement point covers every write path (settings forms,
        onboarding, admin) so the existing view-level (``_can_view_profile``)
        and model-level (``can_view_contact_info``) visibility checks work
        correctly with no changes of their own.
        """
        update_fields = kwargs.get("update_fields")
        if not self.community_enabled:
            forced = [field for field in _COMMUNITY_GATED_VISIBILITY_FIELDS if getattr(self, field) != VisibilityChoice.NO_ONE]
            for field in forced:
                setattr(self, field, VisibilityChoice.NO_ONE)
            if forced and update_fields is not None:
                kwargs["update_fields"] = [*update_fields, *forced]
        super().save(*args, **kwargs)

    @property
    def is_pending_deletion(self) -> bool:
        """True while this account is soft-deleted and awaiting the hard delete."""
        return self.deletion_requested_at is not None

    @property
    def deletion_scheduled_for(self) -> datetime.datetime | None:
        """When the hard delete will run, or None if deletion isn't pending."""
        if self.deletion_requested_at is None:
            return None
        return self.deletion_requested_at + ACCOUNT_DELETION_GRACE_PERIOD

    @property
    def deletion_days_remaining(self) -> int | None:
        """Whole days left before the hard delete runs, or None if not pending.

        Rounds up so "a few hours left" still reads as 1 day rather than 0.
        """
        scheduled_for = self.deletion_scheduled_for
        if scheduled_for is None:
            return None
        remaining_seconds = (scheduled_for - timezone.now()).total_seconds()
        return max(0, math.ceil(remaining_seconds / 86_400))

    @property
    def show_onboarding_tips(self) -> bool:
        """Whether contextual walkthrough cards should be shown."""
        return self.guidance_level == GuidanceLevel.ALL

    @property
    def show_hover_tooltips(self) -> bool:
        """Whether button hover/focus hints should be shown."""
        return self.guidance_level != GuidanceLevel.NONE

    def _best_known_point(self) -> tuple[float, float] | None:
        """Return a representative (lat, lng) for this profile without extra computation.

        Uses already-persisted coordinates only - the explicit custom center, the
        cached pin centroid, or the last remembered map position - so it is cheap
        and side-effect free (it never triggers the O(n²) centroid computation).

        Returns:
            A (latitude, longitude) tuple, or None if no coordinate is on record.
        """
        for lat, lng in (
            (self.map_custom_latitude, self.map_custom_longitude),
            (self.map_center_latitude, self.map_center_longitude),
            (self.remembered_map_lat, self.remembered_map_lng),
        ):
            if lat is not None and lng is not None:
                return float(lat), float(lng)
        return None

    @property
    def effective_distance_units(self) -> str:
        """Return the distance unit to display for this profile.

        An explicit ``distance_units`` choice always wins. Otherwise the unit is
        inferred from the profile's known location, defaulting to kilometres when
        the location is unknown or not in a miles-using region.

        Returns:
            A ``DistanceUnit`` value ("km" or "mi").
        """
        if self.distance_units:
            return self.distance_units
        point = self._best_known_point()
        if point is not None:
            return _units_for_point(*point)
        return DistanceUnit.KILOMETERS

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
        from urbanlens.dashboard.models.pin.model import Pin

        # A Pin's coordinates live on its linked Location (see AddressableModel).
        rows = list(Pin.objects.filter(profile=self).values_list("location__latitude", "location__longitude"))
        pts = [(float(lat), float(lng)) for lat, lng in rows if lat is not None and lng is not None]
        if not pts:
            return None

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

    @staticmethod
    def are_friends(subject: Profile, other: Profile) -> bool:
        """Return True when the two profiles share an accepted friendship.

        Args:
            subject: One profile of the pair.
            other: The other profile.

        Returns:
            True when an accepted Friendship row exists in either direction.
        """
        from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus

        return Friendship.objects.filter(
            models.Q(from_profile=subject, to_profile=other) | models.Q(from_profile=other, to_profile=subject),
            status=FriendshipStatus.ACCEPTED,
        ).exists()

    @staticmethod
    def has_pending_request_to(sender: Profile, recipient: Profile) -> bool:
        """Return True when ``sender`` has an unanswered friend request to ``recipient``.

        Sending a friend request deliberately opens the sender's own privacy
        gates to the recipient - one way only - so the recipient can look at
        who is asking before deciding (see :meth:`visibility_permits`).

        Args:
            sender: The profile that sent the request.
            recipient: The profile the request was sent to.

        Returns:
            True when an unanswered Friendship row exists from sender to recipient.
        """
        from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus

        return Friendship.objects.filter(
            from_profile=sender,
            to_profile=recipient,
            status__in=(FriendshipStatus.REQUESTED, FriendshipStatus.PENDING),
        ).exists()

    @staticmethod
    def _have_common_pin(subject: Profile, other: Profile) -> bool:
        """Return True when both profiles have pinned at least one shared Location.

        Args:
            subject: One profile of the pair.
            other: The other profile.

        Returns:
            True when the profiles' pinned location sets intersect.
        """
        from urbanlens.dashboard.models.pin.model import Pin

        my_locs = set(
            Pin.objects.filter(profile=subject, location__isnull=False).values_list("location_id", flat=True),
        )
        their_locs = set(
            Pin.objects.filter(profile=other, location__isnull=False).values_list("location_id", flat=True),
        )
        return bool(my_locs & their_locs)

    @staticmethod
    def _have_common_friend(subject: Profile, other: Profile) -> bool:
        """Return True when the two profiles share at least one mutual accepted friend.

        Args:
            subject: One profile of the pair.
            other: The other profile.

        Returns:
            True when the profiles' accepted-friend sets intersect.
        """
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

    @staticmethod
    def _have_common_trip(subject: Profile, other: Profile) -> bool:
        """Return True when the two profiles are members of at least one shared trip.

        Args:
            subject: One profile of the pair.
            other: The other profile.

        Returns:
            True when the profiles' trip-membership sets intersect.
        """
        from urbanlens.dashboard.models.trips.model import TripMembership

        my_trips = set(TripMembership.objects.filter(profile=subject).values_list("trip_id", flat=True))
        their_trips = set(TripMembership.objects.filter(profile=other).values_list("trip_id", flat=True))
        return bool(my_trips & their_trips)

    @staticmethod
    def visibility_permits(visibility: str, subject: Profile, other: Profile) -> bool:
        """Return True if ``subject``'s ``visibility`` setting permits ``other``.

        Shared evaluator for every per-field ``VisibilityChoice`` setting on
        this model (contact info, profile, photos, etc.) so the friend/common-pin/
        common-friend/common-trip relationship queries live in exactly one place.

        Accepted friends qualify for every option except NO_ONE - a friend is
        never more of a stranger than someone who merely shares a pin or trip.
        A pending friend request *sent by* ``subject`` counts the recipient as
        a friend too (one way only): asking someone to connect deliberately
        lets them see who is asking.

        Args:
            visibility: The ``VisibilityChoice`` value being evaluated.
            subject: The profile whose setting is being checked.
            other: The profile requesting access.

        Returns:
            True when ``other`` satisfies the visibility requirement.
        """
        if visibility == VisibilityChoice.ANYONE:
            return True
        if visibility == VisibilityChoice.NO_ONE:
            return False
        if Profile.are_friends(subject, other) or Profile.has_pending_request_to(subject, other):
            return True
        if visibility == VisibilityChoice.FRIENDS:
            return False
        if visibility == VisibilityChoice.COMMON_PIN:
            return Profile._have_common_pin(subject, other)
        if visibility == VisibilityChoice.COMMON_FRIEND:
            return Profile._have_common_friend(subject, other)
        if visibility == VisibilityChoice.COMMON_TRIP:
            return Profile._have_common_trip(subject, other)
        if visibility == VisibilityChoice.ANYTHING_IN_COMMON:
            return Profile._have_common_pin(subject, other) or Profile._have_common_friend(subject, other) or Profile._have_common_trip(subject, other)
        return False

    def can_view_photos_from(self, uploader: Profile) -> bool:
        """Return True if this profile is allowed to see photos uploaded by ``uploader``.

        Both directions are enforced:
        - The uploader's ``photo_upload_visibility`` must permit this viewer.
        - This viewer's ``viewer_photo_filter`` must permit photos from the uploader.
        """
        if self == uploader:
            return True

        # Uploader must allow this viewer.
        if not self.visibility_permits(uploader.photo_upload_visibility, uploader, self):
            return False
        # This viewer's filter must allow the uploader.
        return self.visibility_permits(self.viewer_photo_filter, self, uploader)

    def can_view_contact_info(self, viewer: Profile | None) -> bool:
        """Return True if viewer may see this profile's contact methods.

        Args:
            viewer: The profile requesting access, or None for anonymous visitors.

        Returns:
            True when the viewer passes the contact_visibility setting.
        """
        if viewer is not None and self.pk == viewer.pk:
            return True
        if self.contact_visibility == VisibilityChoice.ANYONE:
            return True
        if viewer is None:
            return False
        return self.visibility_permits(self.contact_visibility, self, viewer)

    def accepts_direct_messages_from(self, sender: Profile) -> bool:
        """Return True if ``sender`` may send this profile a direct message.

        Evaluates this profile's ``direct_message_visibility`` setting through
        the shared ``visibility_permits`` evaluator, with one addition: a
        profile that has already messaged the sender can always be replied to,
        regardless of the setting - starting a conversation is an implicit
        invitation to answer.

        Args:
            sender: The profile attempting to send a message.

        Returns:
            True when the sender passes the direct_message_visibility setting
            or this profile previously messaged the sender.
        """
        if self.pk == sender.pk:
            return False
        if self.visibility_permits(self.direct_message_visibility, self, sender):
            return True

        from urbanlens.dashboard.models.direct_messages.model import DirectMessage

        return DirectMessage.objects.filter(sender=self, recipient=sender).exists()

    def can_view_profile(self, viewer: Profile | None) -> bool:
        """Return True if viewer may see this profile's identity (name, etc).

        Args:
            viewer: The profile requesting access, or None for anonymous visitors.

        Returns:
            True when the viewer passes the profile_visibility setting, or
            holds an active temporary access grant (e.g. from an `@friend`
            recommendation in chat - see `DirectMessageTemporaryAccess`).
        """
        if viewer is not None and self.pk == viewer.pk:
            return True
        if self.profile_visibility == VisibilityChoice.ANYONE:
            return True
        if viewer is None:
            return False
        if self.visibility_permits(self.profile_visibility, self, viewer):
            return True

        from urbanlens.dashboard.models.direct_messages.temporary_access import DirectMessageTemporaryAccess

        return DirectMessageTemporaryAccess.grants_access(self.pk, viewer.pk)

    def __str__(self):
        return self.username

    class Meta(abstract.PublicDashboardModel.Meta):
        db_table = "dashboard_profiles"

        indexes = [
            Index(fields=["user"], name="idxdb_profile_user"),
        ]
