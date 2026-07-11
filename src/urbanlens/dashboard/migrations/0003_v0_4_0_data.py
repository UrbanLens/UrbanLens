# v0.4.0 upgrade, part 2 of 4: data backfills only - no DDL. Every operation
# here is a RunPython; the schema it needs was created in 0002 and the columns
# it reads from are not dropped until 0004. See 0002 for the layout rationale.
#
# Ported from the abandoned v0.4.0 chain (migrations/old/), with fixes:
#   - review.profile is backfilled from review.user (the old chain forgot
#     this and its NOT NULL alter would have failed on any existing review).
#   - Legacy location detail pins are converted into child Wiki markers (the
#     old chain silently dropped the pin->wiki linkage).
#   - Campus rows are converted into Boundary rows (the old chain used
#     RenameModel; this chain creates Boundary fresh and deletes Campus).
#   - MarkupMap snapshots write canonical layer_mode values directly
#     (street/topographic), so no post-hoc canonicalization pass is needed.
#   - The old chain's backfill of tos_accepted_at for onboarded profiles is
#     intentionally absent: welcome_onboarding_complete is created in 0002
#     with default False, so no row can match; existing users accept the ToS
#     for real on their next visit to /welcome/.
import hashlib
import logging
import math
from decimal import Decimal

import django.db.migrations.operations.special
from django.contrib.gis.geos import Point
from django.db import migrations
from django.db.models import F

logger = logging.getLogger(__name__)

# Smallest representable step at decimal_places=6 (~0.1 m). Used when nudging
# colliding coordinates apart so distinct rows resolve to distinct Locations.
COORD_STEP = Decimal("0.000001")

_SECURITY_FIELDS = ("fences", "alarms", "cameras", "security", "signs", "vps", "plywood", "locked")

# Client snapshot blobs stored the pre-rename layer names; MarkupMap rows are
# created with the canonical ones.
_LAYER_MODE_CANONICAL = {
    "standard": "street",
    "street": "street",
    "topo": "topographic",
    "topographic": "topographic",
    "satellite": "satellite",
    "dark": "dark",
}


def _quantize_coord(value) -> Decimal:
    return Decimal(str(value)).quantize(COORD_STEP)


def _point_for(latitude: Decimal, longitude: Decimal) -> Point:
    return Point(float(longitude), float(latitude), srid=4326)


# ---------------------------------------------------------------------------
# 1. Pin coordinates: fill NULLs from the linked Location (or legacy point),
#    and nudge same-profile collisions apart so each pin later resolves to a
#    distinct Location, satisfying the new (location, profile) unique
#    constraint added in 0005.
# ---------------------------------------------------------------------------

def backfill_pin_coordinates(apps, schema_editor):
    Pin = apps.get_model("dashboard", "Pin")
    # Process rows with existing coordinates first so they claim their slot in
    # `seen` before NULL rows are backfilled and (if necessary) nudged.
    pins = (
        Pin.objects.select_related("location")
        .only(
            "latitude",
            "longitude",
            "point",
            "profile",
            "parent_pin",
            "parent_location",
            "location__latitude",
            "location__longitude",
        )
        .order_by(F("latitude").asc(nulls_last=True), F("longitude").asc(nulls_last=True), "pk")
    )
    updated = []
    # Coordinates used to be nullable and Postgres treats NULLs as distinct, so
    # a profile could hold several pins that now backfill to identical non-null
    # coordinates (e.g. duplicates sharing a Location, or the 0,0 fallback).
    # Unlike the old chain, pins with parent_location count as roots here: the
    # parent_location column is dropped in 0004, which turns them into root
    # pins subject to the (location, profile) unique constraint.
    seen: set[tuple[int | None, Decimal, Decimal]] = set()
    for pin in pins.iterator():
        latitude = pin.latitude
        longitude = pin.longitude

        if latitude is None and pin.location_id and pin.location.latitude is not None:
            latitude = pin.location.latitude
        if longitude is None and pin.location_id and pin.location.longitude is not None:
            longitude = pin.location.longitude

        if (latitude is None or longitude is None) and pin.point:
            latitude = latitude if latitude is not None else pin.point.y
            longitude = longitude if longitude is not None else pin.point.x

        # Legacy rows without a location or point should not exist for normal
        # pins, but use the historical PointField default so the upgrade can
        # complete instead of failing mid-deploy.
        latitude = latitude if latitude is not None else 0
        longitude = longitude if longitude is not None else 0

        latitude = _quantize_coord(latitude)
        longitude = _quantize_coord(longitude)

        is_root = pin.parent_pin_id is None
        if is_root:
            while (pin.profile_id, latitude, longitude) in seen:
                longitude += COORD_STEP
            seen.add((pin.profile_id, latitude, longitude))

        point = _point_for(latitude, longitude)
        if pin.latitude != latitude or pin.longitude != longitude or pin.point != point:
            pin.latitude = latitude
            pin.longitude = longitude
            pin.point = point
            updated.append(pin)

    if updated:
        Pin.objects.bulk_update(updated, ["latitude", "longitude", "point"], batch_size=500)


# ---------------------------------------------------------------------------
# 2. Reviews: profile is the new owner column; fill it from the legacy user FK
#    before 0004 drops review.user and makes review.profile NOT NULL.
# ---------------------------------------------------------------------------

def backfill_review_profiles(apps, schema_editor):
    Review = apps.get_model("dashboard", "Review")
    Profile = apps.get_model("dashboard", "Profile")

    profile_by_user = dict(Profile.objects.values_list("user_id", "pk"))
    updated = []
    for review in Review.objects.filter(profile__isnull=True).iterator():
        profile_id = profile_by_user.get(review.user_id)
        if profile_id is None:
            # A user without a Profile cannot own app data; the review is
            # unreachable in the UI, so drop it rather than fail the NOT NULL
            # alter in 0004.
            logger.warning("review backfill: dropping review %s (user %s has no profile)", review.pk, review.user_id)
            review.delete()
            continue
        review.profile_id = profile_id
        updated.append(review)

    if updated:
        Review.objects.bulk_update(updated, ["profile"], batch_size=500)


# ---------------------------------------------------------------------------
# 3. Community wikis: extract Location's community fields into Wiki rows and
#    repoint comments / markup / images. Runs before 0004 strips those fields
#    off Location.
# ---------------------------------------------------------------------------

def _has_community_content(loc) -> bool:
    name = (loc.name or "").strip()
    if name and name != "Unnamed Location":
        return True
    if (loc.description or "").strip():
        return True
    if loc.date_abandoned or loc.date_last_active:
        return True
    if any(getattr(loc, f, "unknown") not in (None, "", "unknown") for f in _SECURITY_FIELDS):
        return True
    if loc.badges.exists():
        return True
    if loc.aliases.exists() or loc.edits.exists() or loc.comments.exists():
        return True
    return bool(loc.location_detail_pins.exists() or loc.markup_items.exists() or loc.images.exists())


def backfill_wikis(apps, schema_editor):
    Location = apps.get_model("dashboard", "Location")
    Wiki = apps.get_model("dashboard", "Wiki")
    WikiAlias = apps.get_model("dashboard", "WikiAlias")
    WikiEdit = apps.get_model("dashboard", "WikiEdit")

    for loc in Location.objects.all().iterator():
        if not _has_community_content(loc):
            continue

        wiki, created = Wiki.objects.get_or_create(
            location=loc,
            defaults={
                "name": (loc.name or loc.official_name or "Unnamed Location"),
                "description": loc.description,
                "date_abandoned": loc.date_abandoned,
                "date_last_active": loc.date_last_active,
                **{f: getattr(loc, f, "unknown") for f in _SECURITY_FIELDS},
            },
        )
        if not created:
            continue

        # Shared taxonomy.
        wiki.badges.set(loc.badges.all())

        # Aliases -> WikiAlias.
        for alias in loc.aliases.all():
            WikiAlias.objects.create(
                wiki=wiki,
                name=alias.name,
                created_by=alias.created_by,
            )

        # Edit history -> WikiEdit (two-pass so reverted_by self-links resolve).
        edit_map: dict[int, object] = {}
        for edit in loc.edits.all().order_by("created"):
            new_edit = WikiEdit.objects.create(
                wiki=wiki,
                changes=edit.changes,
                reverted=edit.reverted,
                editor=edit.editor,
            )
            edit_map[edit.id] = new_edit
        for edit in loc.edits.all():
            if edit.reverted_by_id and edit.reverted_by_id in edit_map and edit.id in edit_map:
                target = edit_map[edit.id]
                target.reverted_by = edit_map[edit.reverted_by_id]
                target.save(update_fields=["reverted_by"])

        # Repoint related rows from Location to Wiki. (Legacy location detail
        # pins are handled separately by convert_detail_pins_to_child_wikis.)
        loc.comments.all().update(wiki=wiki)
        loc.markup_items.all().update(parent_wiki=wiki)
        loc.images.all().update(wiki=wiki)


# ---------------------------------------------------------------------------
# 4. Legacy location detail pins -> child Wiki markers. In the new model,
#    shared detail markers on a location's map are child Wikis (Wiki.parent_wiki
#    + pin_type/color/icon), not Pins. The pin rows themselves are kept: after
#    0004 drops parent_location they become ordinary pins for their owner, at
#    coordinates already de-duplicated by backfill_pin_coordinates.
# ---------------------------------------------------------------------------

def convert_detail_pins_to_child_wikis(apps, schema_editor):
    Pin = apps.get_model("dashboard", "Pin")
    Wiki = apps.get_model("dashboard", "Wiki")
    Location = apps.get_model("dashboard", "Location")

    for pin in Pin.objects.filter(parent_location__isnull=False).select_related("parent_location").iterator():
        parent_wiki = Wiki.objects.filter(location_id=pin.parent_location_id).order_by("pk").first()
        if parent_wiki is None:
            # backfill_wikis creates a wiki for every location with detail
            # pins, so this should be unreachable; skip rather than crash.
            logger.warning("detail-pin conversion: no wiki for location %s (pin %s)", pin.parent_location_id, pin.pk)
            continue

        latitude = _quantize_coord(pin.latitude if pin.latitude is not None else pin.parent_location.latitude)
        longitude = _quantize_coord(pin.longitude if pin.longitude is not None else pin.parent_location.longitude)
        # Wiki.location is a OneToOne, so each child wiki needs its own
        # Location row; Location itself is unique on (latitude, longitude).
        while Location.objects.filter(latitude=latitude, longitude=longitude).exists():
            longitude += COORD_STEP
        location = Location.objects.create(
            official_name=pin.name or pin.official_name or None,
            latitude=latitude,
            longitude=longitude,
            point=_point_for(latitude, longitude),
        )
        Wiki.objects.create(
            location=location,
            parent_wiki=parent_wiki,
            name=pin.name or pin.official_name or "Unnamed",
            description=pin.description,
            pin_type=pin.pin_type,
            color=pin.color,
            icon=pin.icon,
            detail_bg_color=pin.detail_bg_color,
            detail_bg_opacity=pin.detail_bg_opacity,
            detail_border_color=pin.detail_border_color,
            detail_border_opacity=pin.detail_border_opacity,
            created_by_id=pin.profile_id,
            date_abandoned=pin.date_abandoned,
            date_last_active=pin.date_last_active,
            **{f: getattr(pin, f, "unknown") for f in _SECURITY_FIELDS},
        )


# ---------------------------------------------------------------------------
# 5. Pin locations: every pin needs a Location before 0004 makes the FK
#    required (and drops pin.latitude/longitude/point).
# ---------------------------------------------------------------------------

def _get_or_create_location(location_model, latitude, longitude, *, official_name=None):
    """Return an existing Location at these coords or create one."""
    latitude = _quantize_coord(latitude)
    longitude = _quantize_coord(longitude)
    location = location_model.objects.filter(latitude=latitude, longitude=longitude).first()
    if location is not None:
        return location
    return location_model.objects.create(
        official_name=official_name,
        latitude=latitude,
        longitude=longitude,
        point=_point_for(latitude, longitude),
    )


def backfill_pin_locations(apps, schema_editor):
    Pin = apps.get_model("dashboard", "Pin")
    Location = apps.get_model("dashboard", "Location")

    updated: list[object] = []
    for pin in Pin.objects.filter(location__isnull=True).iterator():
        official_name = pin.official_name or None
        if not official_name and not pin.is_private:
            official_name = pin.name or None
        location = _get_or_create_location(
            Location,
            pin.latitude,
            pin.longitude,
            official_name=official_name,
        )
        pin.location_id = location.pk
        updated.append(pin)

    if updated:
        Pin.objects.bulk_update(updated, ["location"], batch_size=500)


def backfill_pin_wiki(apps, schema_editor):
    """Link public pins to their community wiki when one exists for the Location."""
    Pin = apps.get_model("dashboard", "Pin")
    Wiki = apps.get_model("dashboard", "Wiki")

    updated: list[object] = []
    for pin in Pin.objects.filter(wiki__isnull=True, is_private=False, location__isnull=False).iterator():
        wiki = Wiki.objects.filter(location_id=pin.location_id).order_by("pk").first()
        if wiki is None:
            continue
        pin.wiki_id = wiki.pk
        updated.append(pin)

    if updated:
        Pin.objects.bulk_update(updated, ["wiki"], batch_size=500)


# ---------------------------------------------------------------------------
# 6. Campus -> Boundary. Legacy default rows (pin=None, profile=None) carried
#    both the API-generated polygon and any community-drawn polygon: the
#    generated geometry becomes a location-default Boundary and a drawn
#    polygon moves to a wiki-keyed row. Legacy pin rows keep their user
#    drawing pin-keyed; their generated cache merges into the location
#    default. All rows are typed PROPERTY - historic campus polygons served
#    as property boundaries, and ambiguity resolves to property.
# ---------------------------------------------------------------------------

def convert_campus_to_boundaries(apps, schema_editor):
    Campus = apps.get_model("dashboard", "Campus")
    Boundary = apps.get_model("dashboard", "Boundary")
    Wiki = apps.get_model("dashboard", "Wiki")

    def merge_generated_into_default(location_id, generated_polygon, generated_at, radius):
        default, created = Boundary.objects.get_or_create(
            location_id=location_id,
            boundary_type="property",
            pin=None,
            wiki=None,
            profile=None,
            defaults={
                "generated_polygon": generated_polygon,
                "generated_at": generated_at,
                "default_radius_meters": radius,
            },
        )
        if not created and default.generated_polygon is None and generated_polygon is not None:
            Boundary.objects.filter(pk=default.pk).update(generated_polygon=generated_polygon, generated_at=generated_at)

    def wiki_for(campus):
        wiki = Wiki.objects.filter(location_id=campus.location_id).order_by("pk").first()
        if wiki is None:
            loc = campus.location
            wiki = Wiki.objects.create(
                location_id=campus.location_id,
                name=(loc.name or loc.official_name or "Unnamed Location"),
            )
        return wiki

    # Default rows first, so pin/profile rows merge into them afterwards.
    defaults = Campus.objects.filter(pin__isnull=True, profile__isnull=True).select_related("location")
    for campus in defaults.iterator():
        merge_generated_into_default(
            campus.location_id,
            campus.generated_polygon,
            campus.updated if campus.generated_polygon is not None else None,
            campus.default_radius_meters,
        )
        if campus.polygon is not None:
            Boundary.objects.create(
                wiki_id=wiki_for(campus).pk,
                location_id=campus.location_id,
                boundary_type="property",
                polygon=campus.polygon,
                default_radius_meters=campus.default_radius_meters,
            )

    # Pin-scoped rows: the user drawing stays pin-keyed; generated geometry
    # was pure cache and merges into the location default.
    for campus in Campus.objects.filter(pin__isnull=False).iterator():
        if campus.generated_polygon is not None:
            merge_generated_into_default(campus.location_id, campus.generated_polygon, campus.updated, campus.default_radius_meters)
        if campus.polygon is not None:
            Boundary.objects.create(
                pin_id=campus.pin_id,
                location_id=campus.location_id,
                boundary_type="property",
                polygon=campus.polygon,
                default_radius_meters=campus.default_radius_meters,
            )

    # Profile-scoped legacy rows (pre pin-scoping); none are expected, but
    # preserve any drawing rather than silently losing it.
    for campus in Campus.objects.filter(pin__isnull=True, profile__isnull=False).iterator():
        if campus.generated_polygon is not None:
            merge_generated_into_default(campus.location_id, campus.generated_polygon, campus.updated, campus.default_radius_meters)
        if campus.polygon is not None:
            Boundary.objects.create(
                profile_id=campus.profile_id,
                location_id=campus.location_id,
                boundary_type="property",
                polygon=campus.polygon,
                default_radius_meters=campus.default_radius_meters,
            )


# ---------------------------------------------------------------------------
# 7. Markup maps: convert check-in route markup and comment/trip-comment map
#    snapshots into MarkupMap rows before 0004 drops the source columns.
# ---------------------------------------------------------------------------

def _haversine_meters(lat1, lng1, lat2, lng2):
    """Great-circle distance in metres between two lat/lng points."""
    radius = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def _as_float(value):
    """Return value as a float, or None when not numeric."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _shape_to_item_fields(shape):
    """Convert a client snapshot shape dict into PinMarkup field values.

    Mirrors ``PinMarkup.from_snapshot_shape`` (inlined - migrations must not
    import live app code). Returns a dict of field values or None when the
    shape cannot be converted.
    """
    if not isinstance(shape, dict):
        return None
    shape_type = shape.get("type")
    latlngs = shape.get("latlngs") or []
    geometry = None
    markup_type = None

    try:
        if shape_type in ("line", "arrow") and len(latlngs) >= 2:
            markup_type = shape_type
            geometry = {"type": "LineString", "coordinates": [[ll[1], ll[0]] for ll in latlngs]}
        elif shape_type == "text" and latlngs:
            markup_type = "text"
            geometry = {"type": "Point", "coordinates": [latlngs[0][1], latlngs[0][0]]}
            if len(latlngs) > 1:
                geometry["box_corner"] = [latlngs[1][1], latlngs[1][0]]
        elif shape_type == "circle" and len(latlngs) >= 2:
            markup_type = "circle"
            (lat1, lng1), (lat2, lng2) = latlngs[0], latlngs[1]
            geometry = {
                "type": "Circle",
                "coordinates": [lng1, lat1],
                "radius": _haversine_meters(lat1, lng1, lat2, lng2),
            }
        elif shape_type == "rect" and len(latlngs) >= 2:
            markup_type = "square"
            north, west = latlngs[0]
            south, east = latlngs[1]
            geometry = {
                "type": "Polygon",
                "coordinates": [[[west, north], [east, north], [east, south], [west, south], [west, north]]],
            }
        elif shape_type == "polygon" and len(latlngs) >= 3:
            markup_type = "polygon"
            ring = [[ll[1], ll[0]] for ll in latlngs]
            ring.append(ring[0])
            geometry = {"type": "Polygon", "coordinates": [ring]}
    except (TypeError, IndexError, ValueError):
        return None

    if markup_type is None or geometry is None:
        return None

    stroke_width = shape.get("stroke_width")
    try:
        stroke_width = int(stroke_width) if stroke_width is not None else (16 if markup_type == "text" else 3)
    except (TypeError, ValueError):
        stroke_width = 16 if markup_type == "text" else 3

    def _opacity(key, default):
        try:
            return int(float(shape.get(key, default)))
        except (TypeError, ValueError):
            return default

    return {
        "markup_type": markup_type,
        "geometry": geometry,
        "label": str(shape.get("label") or ""),
        "color": shape.get("color") or "#e53e3e",
        "stroke_width": stroke_width,
        "border_color": shape.get("border_color") or "",
        "fill_opacity": _opacity("fill_opacity", 87),
        "border_opacity": _opacity("border_opacity", 100),
    }


def _map_from_snapshot(markup_map_model, pin_markup_model, profile_id, snapshot):
    """Create a MarkupMap (+items) from a stored snapshot blob; returns its id or None."""
    if not isinstance(snapshot, dict):
        return None
    center_lat = _as_float(snapshot.get("center_lat"))
    center_lng = _as_float(snapshot.get("center_lng"))
    markup_map = markup_map_model.objects.create(
        profile_id=profile_id,
        center_latitude=center_lat,
        center_longitude=center_lng,
        zoom=_as_float(snapshot.get("zoom")) or 13,
        layer_mode=_LAYER_MODE_CANONICAL.get(snapshot.get("layer_mode"), "street"),
        show_borders=bool(snapshot.get("show_borders")),
    )
    for shape in snapshot.get("markup") or []:
        fields = _shape_to_item_fields(shape)
        if fields is None:
            continue
        pin_markup_model.objects.create(parent_map_id=markup_map.id, profile_id=profile_id, **fields)
    return markup_map.id


def convert_markup_snapshots(apps, schema_editor):
    """Convert check-in markup and snapshot blobs into MarkupMap rows."""
    markup_map_model = apps.get_model("dashboard", "MarkupMap")
    pin_markup_model = apps.get_model("dashboard", "PinMarkup")
    safety_checkin_model = apps.get_model("dashboard", "SafetyCheckin")
    comment_model = apps.get_model("dashboard", "Comment")
    trip_comment_model = apps.get_model("dashboard", "TripComment")

    # 1. Check-in route markup: one MarkupMap per check-in that has items.
    checkin_ids = (
        pin_markup_model.objects.filter(parent_safety_checkin__isnull=False)
        .values_list("parent_safety_checkin_id", flat=True)
        .distinct()
    )
    for checkin in safety_checkin_model.objects.filter(id__in=list(checkin_ids)):
        markup_map = markup_map_model.objects.create(
            profile_id=checkin.profile_id,
            center_latitude=_as_float(checkin.destination_latitude),
            center_longitude=_as_float(checkin.destination_longitude),
            zoom=13,
        )
        pin_markup_model.objects.filter(parent_safety_checkin_id=checkin.id).update(parent_map_id=markup_map.id)
        checkin.markup_map_id = markup_map.id
        checkin.save(update_fields=["markup_map"])

    # 2. Snapshot blobs on comments and trip comments. (PinVisit.map_data
    # never existed in this chain, so there is nothing to convert there.)
    for comment in comment_model.objects.filter(map_data__isnull=False).iterator():
        map_id = _map_from_snapshot(markup_map_model, pin_markup_model, comment.profile_id, comment.map_data)
        if map_id is not None:
            comment.markup_map_id = map_id
            comment.save(update_fields=["markup_map"])

    for trip_comment in trip_comment_model.objects.filter(map_data__isnull=False).exclude(author__isnull=True).iterator():
        map_id = _map_from_snapshot(markup_map_model, pin_markup_model, trip_comment.author_id, trip_comment.map_data)
        if map_id is not None:
            trip_comment.markup_map_id = map_id
            trip_comment.save(update_fields=["markup_map"])


# ---------------------------------------------------------------------------
# 8. Small backfills.
# ---------------------------------------------------------------------------

# Default safety-preference messages that shipped in earlier releases. Rows
# still holding one of these were never customized by the user and follow the
# default forward; anything else is user text and is left alone.
_HISTORICAL_DEFAULT_MESSAGES = (
    "Hi! You're specified as one of my emergency contacts in case I get lost or injured. If you're seeing this, "
    "I didn't make it home when I expected to, so this email was automatically sent to you. Please try to reach me, "
    "and if you can't, information about my trip plan is included to help figure out where I might be.",
    "Hi! I'm heading out and set up this automatic check-in as a precaution. If you're seeing this, "
    "I haven't checked in by my expected time - please try to reach me, and if you can't, take a look "
    "at my trip plan and last known destination below to help figure out where I might be.",
)

_CURRENT_DEFAULT_MESSAGE = (
    "Hi! I went on a trip and set up an automated safety check-in: if I don't confirm I'm safe by my expected "
    "return time, this message is sent to my emergency contacts. If you're reading this, I didn't come home and "
    "may need help - please try to reach me, and if you can't, use the trip plan included with this alert to help "
    "find me. I may not be able to use my phone to contact anyone else for help, so it's important you try to help me."
)


def upgrade_unchanged_defaults(apps, schema_editor):
    """Swap the current default text into preferences the user never customized."""
    SafetyPreference = apps.get_model("dashboard", "SafetyPreference")
    SafetyPreference.objects.filter(default_message__in=_HISTORICAL_DEFAULT_MESSAGES).update(default_message=_CURRENT_DEFAULT_MESSAGE)


def backfill_vip_storage_quota(apps, schema_editor):
    """Give the pre-existing built-in VIP role its default 500 GB quota.

    Only rows still at NULL are touched, so an admin who later blanks the quota
    (meaning "use the site default") is not re-overridden by ensure_defaults -
    that helper only sets the quota when it creates the role.
    """
    SubscriptionRole = apps.get_model("dashboard", "SubscriptionRole")
    SubscriptionRole.objects.filter(slug="vip", storage_quota_gb__isnull=True).update(storage_quota_gb=500)


def backfill_checksums(apps, schema_editor):
    """Hash existing image files so duplicate detection covers pre-existing photos.

    Files that are missing from storage (or unreadable) are skipped - their rows
    keep a NULL checksum, which the duplicate check ignores; they are also
    backfilled lazily by process_image_upload if ever reprocessed.
    """
    Image = apps.get_model("dashboard", "Image")
    for image in Image.objects.filter(checksum__isnull=True).exclude(image="").iterator():
        try:
            with image.image.open("rb") as fh:
                digest = hashlib.sha256()
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    digest.update(chunk)
            Image.objects.filter(pk=image.pk).update(checksum=digest.hexdigest())
        except OSError as exc:
            logger.warning("checksum backfill: could not read image %s: %s", image.pk, exc)


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0002_boundary_emailsendlog_externalvisitparticipant_and_more"),
    ]

    operations = [
        migrations.RunPython(
            code=backfill_pin_coordinates,
            reverse_code=django.db.migrations.operations.special.RunPython.noop,
        ),
        migrations.RunPython(
            code=backfill_review_profiles,
            reverse_code=django.db.migrations.operations.special.RunPython.noop,
        ),
        migrations.RunPython(
            code=backfill_wikis,
            reverse_code=django.db.migrations.operations.special.RunPython.noop,
        ),
        migrations.RunPython(
            code=convert_detail_pins_to_child_wikis,
            reverse_code=django.db.migrations.operations.special.RunPython.noop,
        ),
        migrations.RunPython(
            code=backfill_pin_locations,
            reverse_code=django.db.migrations.operations.special.RunPython.noop,
        ),
        migrations.RunPython(
            code=backfill_pin_wiki,
            reverse_code=django.db.migrations.operations.special.RunPython.noop,
        ),
        migrations.RunPython(
            code=convert_campus_to_boundaries,
            reverse_code=django.db.migrations.operations.special.RunPython.noop,
        ),
        migrations.RunPython(
            code=convert_markup_snapshots,
            reverse_code=django.db.migrations.operations.special.RunPython.noop,
        ),
        migrations.RunPython(
            code=upgrade_unchanged_defaults,
            reverse_code=django.db.migrations.operations.special.RunPython.noop,
        ),
        migrations.RunPython(
            code=backfill_vip_storage_quota,
            reverse_code=django.db.migrations.operations.special.RunPython.noop,
        ),
        migrations.RunPython(
            code=backfill_checksums,
            reverse_code=django.db.migrations.operations.special.RunPython.noop,
        ),
    ]
