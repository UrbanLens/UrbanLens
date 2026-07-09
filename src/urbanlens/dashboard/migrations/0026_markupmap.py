"""Introduce standalone MarkupMap containers.

Markup previously lived in two disconnected shapes:

- ``PinMarkup.parent_safety_checkin`` rows (server-persisted check-in route markup)
- ``map_data`` JSON snapshot blobs on ``Comment`` / ``TripComment`` / ``PinVisit``

Both become rows of a new ``MarkupMap`` model (viewport + ``PinMarkup`` items via
``parent_map``), which the host models now reference through a nullable
``markup_map`` FK. The data migration converts existing check-in markup and all
stored snapshot blobs, then the old column/field are dropped.
"""

import math
import uuid

import django.db.models.deletion
from django.db import migrations, models

_METERS_PER_DEGREE_LAT = 111_320.0

_LAYER_MODES = {"standard", "satellite", "topo", "dark"}


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
    layer_mode = snapshot.get("layer_mode")
    markup_map = markup_map_model.objects.create(
        profile_id=profile_id,
        center_latitude=center_lat,
        center_longitude=center_lng,
        zoom=_as_float(snapshot.get("zoom")) or 13,
        layer_mode=layer_mode if layer_mode in _LAYER_MODES else "standard",
        show_borders=bool(snapshot.get("show_borders")),
    )
    for shape in snapshot.get("markup") or []:
        fields = _shape_to_item_fields(shape)
        if fields is None:
            continue
        pin_markup_model.objects.create(parent_map_id=markup_map.id, profile_id=profile_id, **fields)
    return markup_map.id


def _forwards(apps, schema_editor):
    """Convert check-in markup and snapshot blobs into MarkupMap rows."""
    markup_map_model = apps.get_model("dashboard", "MarkupMap")
    pin_markup_model = apps.get_model("dashboard", "PinMarkup")
    safety_checkin_model = apps.get_model("dashboard", "SafetyCheckin")
    comment_model = apps.get_model("dashboard", "Comment")
    trip_comment_model = apps.get_model("dashboard", "TripComment")
    pin_visit_model = apps.get_model("dashboard", "PinVisit")

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

    # 2. Snapshot blobs on comments, trip comments, and visits.
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

    for visit in pin_visit_model.objects.filter(map_data__isnull=False).select_related("pin").iterator():
        map_id = _map_from_snapshot(markup_map_model, pin_markup_model, visit.pin.profile_id, visit.map_data)
        if map_id is not None:
            visit.markup_map_id = map_id
            visit.save(update_fields=["markup_map"])


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0025_reset_stale_location_country_default"),
    ]

    operations = [
        migrations.CreateModel(
            name="MarkupMap",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("uuid", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ("title", models.CharField(blank=True, default="", max_length=200)),
                ("center_latitude", models.FloatField(blank=True, null=True)),
                ("center_longitude", models.FloatField(blank=True, null=True)),
                ("zoom", models.FloatField(blank=True, null=True)),
                (
                    "layer_mode",
                    models.CharField(
                        choices=[("standard", "Street"), ("satellite", "Satellite"), ("topo", "Topographic"), ("dark", "Dark")],
                        default="standard",
                        max_length=20,
                    ),
                ),
                ("show_borders", models.BooleanField(default=False)),
                (
                    "profile",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="markup_maps", to="dashboard.profile"),
                ),
            ],
            options={
                "db_table": "dashboard_markup_maps",
                "ordering": ["-updated"],
                "abstract": False,
                "indexes": [
                    models.Index(fields=["uuid"], name="idxdb_mm_uuid"),
                    models.Index(fields=["profile"], name="idxdb_mm_profile"),
                ],
            },
        ),
        migrations.AddField(
            model_name="pinmarkup",
            name="parent_map",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="items",
                to="dashboard.markupmap",
            ),
        ),
        migrations.AddIndex(
            model_name="pinmarkup",
            index=models.Index(fields=["parent_map"], name="idxdb_pm_map"),
        ),
        migrations.AddField(
            model_name="safetycheckin",
            name="markup_map",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="safety_checkins",
                to="dashboard.markupmap",
            ),
        ),
        migrations.AddField(
            model_name="comment",
            name="markup_map",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="comments",
                to="dashboard.markupmap",
            ),
        ),
        migrations.AddField(
            model_name="tripcomment",
            name="markup_map",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="trip_comments",
                to="dashboard.markupmap",
            ),
        ),
        migrations.AddField(
            model_name="pinvisit",
            name="markup_map",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="visits",
                to="dashboard.markupmap",
            ),
        ),
        migrations.RunPython(_forwards, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="pinmarkup",
            name="parent_safety_checkin",
        ),
        migrations.RemoveField(
            model_name="comment",
            name="map_data",
        ),
        migrations.RemoveField(
            model_name="tripcomment",
            name="map_data",
        ),
        migrations.RemoveField(
            model_name="pinvisit",
            name="map_data",
        ),
    ]
