"""Model Bakery recipes for UrbanLens test data.

Usage::

    from model_bakery import baker

    # Profile is always auto-created via User's post_save signal.
    user = baker.make_recipe('dashboard.user')
    profile = user.profile

    # Pass profile explicitly to recipes that need it:
    pin = baker.make_recipe('dashboard.pin', profile=profile)

    # Or let the recipe create a fresh user+profile on its own:
    pin = baker.make_recipe('dashboard.pin')

All recipes that carry a Profile FK use ``_make_profile`` — a callable that
baker invokes fresh for each make() call, ensuring each recipe instance gets
its own User/Profile and avoiding OneToOneField constraint violations.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.contrib.auth.models import User
from model_bakery import baker as _baker
from model_bakery.recipe import Recipe, foreign_key, seq

from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType, Permission
from urbanlens.dashboard.models.markup.model import MarkupType
from urbanlens.dashboard.models.notifications.meta import DeliveryPreference, Importance, NotificationType, Status
from urbanlens.dashboard.models.visits.model import VisitSource


def _make_profile():
    """Create a fresh User and return the Profile auto-created by the post_save signal."""
    return _baker.make(User).profile


# ── Auth ──────────────────────────────────────────────────────────────────────

user = Recipe(
    User,
    username=seq("user_"),
    email=seq("user_", suffix="@example.com"),
    first_name="Test",
    last_name="User",
    is_active=True,
)

# ── Location ──────────────────────────────────────────────────────────────────
# latitude/longitude have a unique_together constraint; seq() ensures each
# recipe call produces a distinct coordinate pair.

location: Recipe[Any] = Recipe(
    "dashboard.Location",
    name=seq("Location "),
    latitude=seq(Decimal("40.001"), increment_by=Decimal("0.001")),
    longitude=seq(Decimal("-74.001"), increment_by=Decimal("0.001")),
)

# ── Badges ────────────────────────────────────────────────────────────────────

badge: Recipe[Any] = Recipe("dashboard.Badge", name=seq("Badge "), kind="tag")
tag_badge: Recipe[Any] = Recipe("dashboard.Badge", name=seq("Tag "), kind="tag")
category_badge: Recipe[Any] = Recipe("dashboard.Badge", name=seq("Category "), kind="category")
status_badge: Recipe[Any] = Recipe("dashboard.Badge", name=seq("Status "), kind="status")

badge_customization: Recipe[Any] = Recipe(
    "dashboard.BadgeCustomization",
    profile=_make_profile,
    badge=foreign_key("dashboard.badge"),
)

# ── Campus ────────────────────────────────────────────────────────────────────

campus: Recipe[Any] = Recipe(
    "dashboard.Campus",
    location=foreign_key("dashboard.location"),
    profile=_make_profile,
    default_radius_meters=50,
)

# Admin campus: profile=None means this is the site-wide default for the location.
admin_campus: Recipe[Any] = Recipe(
    "dashboard.Campus",
    location=foreign_key("dashboard.location"),
    profile=None,
    default_radius_meters=100,
)

# ── Pin ───────────────────────────────────────────────────────────────────────

pin: Recipe[Any] = Recipe(
    "dashboard.Pin",
    profile=_make_profile,
    location=foreign_key("dashboard.location"),
    parent_pin=None,
    parent_location=None,
)

# A pin nested under another pin (detail/sub-pin).
detail_pin: Recipe[Any] = Recipe(
    "dashboard.Pin",
    profile=_make_profile,
    location=foreign_key("dashboard.location"),
    parent_pin=foreign_key("dashboard.pin"),
)

pin_note: Recipe[Any] = Recipe(
    "dashboard.PinNote",
    pin=foreign_key("dashboard.pin"),
    text="A test note.",
)

# ── Visits ────────────────────────────────────────────────────────────────────

pin_visit: Recipe[Any] = Recipe(
    "dashboard.PinVisit",
    pin=foreign_key("dashboard.pin"),
    source=VisitSource.MANUAL,
)

takeout_visit: Recipe[Any] = Recipe(
    "dashboard.PinVisit",
    pin=foreign_key("dashboard.pin"),
    source="takeout",
)

# ── Aliases ───────────────────────────────────────────────────────────────────

pin_alias: Recipe[Any] = Recipe(
    "dashboard.PinAlias",
    pin=foreign_key("dashboard.pin"),
    name=seq("alias_"),
)

location_alias: Recipe[Any] = Recipe(
    "dashboard.LocationAlias",
    location=foreign_key("dashboard.location"),
    name=seq("loc_alias_"),
    created_by=_make_profile,
)

# ── Images ────────────────────────────────────────────────────────────────────

image: Recipe[Any] = Recipe(
    "dashboard.Image",
    pin=foreign_key("dashboard.pin"),
)

# ── Markup ────────────────────────────────────────────────────────────────────

pin_markup: Recipe[Any] = Recipe(
    "dashboard.PinMarkup",
    parent_pin=foreign_key("dashboard.pin"),
    profile=_make_profile,
    markup_type=MarkupType.LINE,
    geometry={"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
    label="",
    color="#e53e3e",
    stroke_width=3,
    fill_opacity=87,
    border_opacity=100,
)

# ── Comments & Reactions ──────────────────────────────────────────────────────

# Pin comment — exactly one of (pin, location) must be set.
comment: Recipe[Any] = Recipe(
    "dashboard.Comment",
    profile=_make_profile,
    pin=foreign_key("dashboard.pin"),
    location=None,
    parent=None,
    text="A test comment.",
)

# Location / wiki comment.
location_comment: Recipe[Any] = Recipe(
    "dashboard.Comment",
    profile=_make_profile,
    pin=None,
    location=foreign_key("dashboard.location"),
    parent=None,
    text="A test location comment.",
)

reaction: Recipe[Any] = Recipe(
    "dashboard.Reaction",
    profile=_make_profile,
    comment=foreign_key("dashboard.comment"),
    trip_comment=None,
    emoji="👍",
)

trip_comment_reaction: Recipe[Any] = Recipe(
    "dashboard.Reaction",
    profile=_make_profile,
    comment=None,
    trip_comment=foreign_key("dashboard.trip_comment"),
    emoji="❤️",
)

# ── Reviews ───────────────────────────────────────────────────────────────────

review: Recipe[Any] = Recipe(
    "dashboard.Review",
    user=foreign_key("dashboard.user"),
    pin=foreign_key("dashboard.pin"),
    rating=4,
    review="A test review.",
)

# ── Friendships ───────────────────────────────────────────────────────────────

friendship: Recipe[Any] = Recipe(
    "dashboard.Friendship",
    from_profile=_make_profile,
    to_profile=_make_profile,
    status=FriendshipStatus.REQUESTED,
    relationship_type=FriendshipType.FRIEND,
    permissions=Permission.VIEW_PROFILE,
)

accepted_friendship: Recipe[Any] = Recipe(
    "dashboard.Friendship",
    from_profile=_make_profile,
    to_profile=_make_profile,
    status=FriendshipStatus.ACCEPTED,
    relationship_type=FriendshipType.FRIEND,
    permissions=Permission.VIEW_PROFILE,
)

# ── Trips ────────────────────────────────────────────────────────────────────

trip: Recipe[Any] = Recipe(
    "dashboard.Trip",
    name=seq("Trip "),
    creator=_make_profile,
)

trip_membership: Recipe[Any] = Recipe(
    "dashboard.TripMembership",
    trip=foreign_key("dashboard.trip"),
    profile=_make_profile,
    is_organizer=False,
)

organizer_membership: Recipe[Any] = Recipe(
    "dashboard.TripMembership",
    trip=foreign_key("dashboard.trip"),
    profile=_make_profile,
    is_organizer=True,
)

trip_activity: Recipe[Any] = Recipe(
    "dashboard.TripActivity",
    trip=foreign_key("dashboard.trip"),
    title=seq("Activity "),
    status="proposed",
    order=0,
    added_by=_make_profile,
    location=None,
    pin=None,
)

trip_comment: Recipe[Any] = Recipe(
    "dashboard.TripComment",
    trip=foreign_key("dashboard.trip"),
    author=_make_profile,
    text="A test trip comment.",
    parent=None,
)

trip_activity_vote: Recipe[Any] = Recipe(
    "dashboard.TripActivityVote",
    activity=foreign_key("dashboard.trip_activity"),
    profile=_make_profile,
    vote="up",
)

# ── Notifications ─────────────────────────────────────────────────────────────

notification_log: Recipe[Any] = Recipe(
    "dashboard.NotificationLog",
    profile=_make_profile,
    status=Status.UNREAD,
    importance=Importance.LOWEST,
    notification_type=NotificationType.INFO,
    title="Test notification",
    message="This is a test notification.",
    url="",
)

# NotificationPreference has a OneToOneField to Profile.
# Note: a NotificationPreference may be auto-created by a post_save signal
# when a Profile is created.  In that case, access it via profile.notification_preferences
# rather than using this recipe directly.
notification_preference: Recipe[Any] = Recipe(
    "dashboard.NotificationPreference",
    profile=_make_profile,
    trip_updated=DeliveryPreference.SITE,
    friend_request=DeliveryPreference.SITE,
    message=DeliveryPreference.SITE,
    comment_reply=DeliveryPreference.SITE,
    comment_liked=DeliveryPreference.NONE,
    friend_accepted=DeliveryPreference.SITE,
    added_to_trip=DeliveryPreference.SITE,
    wiki_updated=DeliveryPreference.NONE,
)

# ── Social Links ──────────────────────────────────────────────────────────────

social_link: Recipe[Any] = Recipe(
    "dashboard.SocialLink",
    profile=_make_profile,
    platform=seq("platform_"),
    handle=seq("handle_"),
)

# ── Location Edit History ─────────────────────────────────────────────────────

location_edit: Recipe[Any] = Recipe(
    "dashboard.LocationEdit",
    location=foreign_key("dashboard.location"),
    editor=_make_profile,
    changes={"name": {"old": "Old Name", "new": "New Name"}},
    reverted=False,
    reverted_by=None,
)

# ── Authentication ────────────────────────────────────────────────────────────

email_verification: Recipe[Any] = Recipe(
    "dashboard.EmailVerification",
    user=foreign_key("dashboard.user"),
    verified_at=None,
)

# ── Geocoding Cache ───────────────────────────────────────────────────────────

geocoded_location: Recipe[Any] = Recipe(
    "dashboard.GeocodedLocation",
    latitude=Decimal("40.000"),
    longitude=Decimal("-74.000"),
    place_name="Test Place",
    json_response=None,
)
