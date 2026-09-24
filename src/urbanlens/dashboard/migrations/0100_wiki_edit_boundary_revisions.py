from django.db import migrations

_LEGACY_KEY = "bounding_box"
_PREFIX = "boundary_"
_TOLERANCE = 1e-8


def _outline(wkt):
    from django.contrib.gis.geos import GEOSGeometry, MultiPolygon, Polygon
    from django.contrib.gis.geos.error import GEOSException

    if not isinstance(wkt, str) or not wkt.strip():
        return None
    try:
        geometry = GEOSGeometry(wkt, srid=4326)
    except (GEOSException, ValueError, TypeError):
        return None
    if isinstance(geometry, Polygon):
        geometry = MultiPolygon(geometry, srid=4326)
    return geometry if isinstance(geometry, MultiPolygon) and not geometry.empty else None


def convert_inline_boundaries(apps, schema_editor):
    """Move inline WKT out of WikiEdit.changes into BoundaryRevision rows named by id."""
    WikiEdit = apps.get_model("dashboard", "WikiEdit")
    BoundaryRevision = apps.get_model("dashboard", "BoundaryRevision")
    latest = {}

    def revision_id(wiki_id, boundary_type, value):
        if value is None or (isinstance(value, int) and not isinstance(value, bool)):
            return value
        polygon = _outline(value)
        if polygon is None:
            return None
        previous = latest.get((wiki_id, boundary_type))
        if previous is not None and previous.polygon.equals_exact(polygon, _TOLERANCE):
            return previous.pk
        revision = BoundaryRevision.objects.create(wiki_id=wiki_id, boundary_type=boundary_type, polygon=polygon)
        latest[(wiki_id, boundary_type)] = revision
        return revision.pk

    for edit in WikiEdit.objects.order_by("wiki_id", "pk").iterator():
        changes = edit.changes if isinstance(edit.changes, dict) else {}
        if not any(key == _LEGACY_KEY or key.startswith(_PREFIX) for key in changes):
            continue
        converted = {}
        for key, diff in changes.items():
            if key != _LEGACY_KEY and not key.startswith(_PREFIX):
                converted[key] = diff
                continue
            if not isinstance(diff, dict):
                continue
            new_key = f"{_PREFIX}property" if key == _LEGACY_KEY else key
            boundary_type = new_key.removeprefix(_PREFIX)
            converted[new_key] = {
                "from": revision_id(edit.wiki_id, boundary_type, diff.get("from")),
                "to": revision_id(edit.wiki_id, boundary_type, diff.get("to")),
            }
        WikiEdit.objects.filter(pk=edit.pk).update(changes=converted)


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0099_boundary_revision")]

    operations = [migrations.RunPython(convert_inline_boundaries, migrations.RunPython.noop)]
