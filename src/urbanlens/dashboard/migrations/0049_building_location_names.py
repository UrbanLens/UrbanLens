"""Building locations drop a campus's name for their own CRIS record's, and their wikis nest under the parcel's (P231, P255).

A building's location is one that only child pins, or a child wiki, stand on (``name_tiers.naming_scope``). On it:

- A Wikipedia name is the campus's article, and a register listing's name counts only for a structure listing
  standing on the building; a CRIS name counts only when the cached record stands on the building. A location
  without a name that counts takes its cached CRIS record's name when that record stands on it; a rejected name
  otherwise gives way to REData's building name, else is cleared. A changed name re-mints the location's and its
  wiki's slugs; a readable slug given up goes to the slug history.
- A cached CRIS building record that does not stand on the building, or has no position to tell, is dropped and
  fetched again under the stand-on rule.
- A root wiki nests under the wiki of the nearest place its own place, or its location's place, sits in.

"Stands on" here is the building's footprint, else within 15 m; the runtime rule also rejects a point another
parcel building is nearer to, which reads a site's building list this migration does not.
"""

from django.contrib.gis.geos import Point
from django.db import migrations
from django.db.models import Exists, OuterRef, Q

from urbanlens.dashboard.services.core.slugs import PREFERRED_CHILD_SLUG_LENGTH, could_mint, is_uuid_slug, parent_slug_prefix, unique_slug
from urbanlens.dashboard.services.locations.naming import is_address_derived_name, is_meaningful_name, normalize_name_for_comparison, sanitize_name
from urbanlens.dashboard.services.locations.site_scope import BUILDING_MATCH_METERS, meters_between

_SLUG_MAX_LENGTH = 255
_CHUNK_SIZE = 500
_MAX_LINEAGE_DEPTH = 16
_CRIS = "cris_building_usn"
_REGISTERS = "redata_historic_registers"
_REDATA_BUILDING = "redata_building_attributes"


def _building_locations(Location, Pin, Wiki):
    root_pin = Pin.objects.filter(location=OuterRef("pk"), parent_pin__isnull=True)
    any_pin = Pin.objects.filter(location=OuterRef("pk"))
    child_wiki = Wiki.objects.filter(location=OuterRef("pk"), parent_wiki__isnull=False)
    return Location.objects.filter(~Exists(root_pin)).filter(Q(Exists(any_pin)) | Q(Exists(child_wiki))).select_related("place").order_by("pk")


def _chunks(queryset):
    chunk = []
    for row in queryset.iterator(chunk_size=_CHUNK_SIZE):
        chunk.append(row)
        if len(chunk) == _CHUNK_SIZE:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def _footprint(location):
    place = location.place
    if place is None or place.kind != "building" or place.geometry is None or place.geometry.empty:
        return None
    return place.geometry


def _stands_on(location, latitude, longitude):
    if latitude is None or longitude is None:
        return False
    try:
        latitude, longitude = float(latitude), float(longitude)
    except (TypeError, ValueError):
        return False
    footprint = _footprint(location)
    if footprint is not None:
        return bool(footprint.contains(Point(longitude, latitude, srid=4326)))
    return meters_between(latitude, longitude, float(location.latitude), float(location.longitude)) <= BUILDING_MATCH_METERS


def _own_listing(location, registers, name):
    target = normalize_name_for_comparison(name)
    rows = (registers or {}).get("resources") if isinstance(registers, dict) else None
    return any(
        isinstance(row, dict) and row.get("scope") != "site" and normalize_name_for_comparison(str(row.get("name") or "")) == target and _stands_on(location, row.get("source_latitude"), row.get("source_longitude"))
        for row in rows or []
    )


def _new_name(location, cris, registers, redata_building):
    """The ``(name, source)`` a building's location should have now, or None to leave it as it is.

    As on a child pin's location at runtime (``name_resolution.default_name_resolver``), the building's own CRIS
    record wins, then REData's building name. REData's replaces only a rejected name; an unnamed location without a
    CRIS record is left to the runtime.
    """
    name, source = location.official_name or "", location.official_name_source or ""
    usn = str(cris.get("USNName") or "").strip() if isinstance(cris, dict) else ""
    stands = bool(usn) and cris.get("site_scope") is not True and _stands_on(location, cris.get("source_latitude"), cris.get("source_longitude"))
    rejected = source == "wikipedia" or (source == "historic_register" and not _own_listing(location, registers, name)) or (source == "cris" and not stands)
    if name and not rejected:
        return None
    if stands and is_meaningful_name(usn):
        return sanitize_name(usn), "cris"
    if not rejected:
        return None
    redata = str(redata_building.get("name") or "").strip() if isinstance(redata_building, dict) else ""
    if is_meaningful_name(redata) and not is_address_derived_name(redata, location):
        return sanitize_name(redata), "redata_building"
    return "", ""


def _misplaced_cris_row(location, cris):
    if not isinstance(cris, dict) or cris.get("site_scope") is True or not (cris.get("USNName") or cris.get("resource_uuid")):
        return False
    return not _stands_on(location, cris.get("source_latitude"), cris.get("source_longitude"))


def _rename_locations(Location, Pin, Wiki, LocationCache, LocationSlugHistory):
    """Rename each building location a chunk at a time; returns the ids whose name changed."""
    renamed = []
    claimed = set()

    def taken(pk, candidate):
        return candidate in claimed or Location.objects.filter(slug=candidate).exclude(pk=pk).exists() or LocationSlugHistory.objects.filter(slug=candidate).exclude(location_id=pk).exists()

    for chunk in _chunks(_building_locations(Location, Pin, Wiki)):
        rows = LocationCache.objects.filter(location_id__in=[location.pk for location in chunk], source__in=(_CRIS, _REGISTERS, _REDATA_BUILDING), audience="").values_list("pk", "location_id", "source", "data")
        cached, row_ids = {}, {}
        for pk, location_id, source, data in rows:
            cached[(location_id, source)] = data
            row_ids[(location_id, source)] = pk

        dropped = []
        for location in chunk:
            cris = cached.get((location.pk, _CRIS))
            if _misplaced_cris_row(location, cris):
                dropped.append(row_ids[(location.pk, _CRIS)])
            decided = _new_name(location, cris, cached.get((location.pk, _REGISTERS)), cached.get((location.pk, _REDATA_BUILDING)))
            if decided is None or decided == (location.official_name or "", location.official_name_source or ""):
                continue
            name, source = decided
            former = location.slug
            if name and could_mint(former or "", name, max_length=_SLUG_MAX_LENGTH):
                slug = former
            elif name:
                slug = unique_slug(name, is_taken=lambda candidate, pk=location.pk: taken(pk, candidate), max_length=_SLUG_MAX_LENGTH)
                claimed.add(slug)
            else:
                slug = str(location.uuid)
            Location.objects.filter(pk=location.pk).update(official_name=name, official_name_source=source, slug=slug)
            LocationSlugHistory.objects.filter(location_id=location.pk, slug=slug).delete()
            if former and former != slug and not is_uuid_slug(former):
                LocationSlugHistory.objects.get_or_create(slug=former, defaults={"location_id": location.pk})
            renamed.append(location.pk)
        LocationCache.objects.filter(pk__in=dropped).delete()
    return renamed


def _provider_name(location):
    return location.official_name if location is not None and location.official_name and location.official_name_source else None


def _remint_wikis(Wiki, location_ids):
    """Re-mint the slug of each renamed location's wiki from its new provider name, or the uuid."""
    wikis = list(Wiki.objects.filter(location_id__in=location_ids).select_related("location", "parent_wiki__location"))
    targets = {}
    for wiki in wikis:
        base = _provider_name(wiki.location)
        if not base:
            targets[wiki.pk] = str(wiki.uuid)
            continue
        parent_name = _provider_name(wiki.parent_wiki.location) if wiki.parent_wiki_id else None
        prefix = parent_slug_prefix([parent_name]) if parent_name else ""
        preferred = PREFERRED_CHILD_SLUG_LENGTH if wiki.parent_wiki_id else _SLUG_MAX_LENGTH
        if not could_mint(wiki.slug or "", base, prefix=prefix, max_length=_SLUG_MAX_LENGTH, preferred_length=preferred):
            targets[wiki.pk] = (base, prefix, preferred)
    moving = [wiki for wiki in wikis if wiki.pk in targets and wiki.slug != targets[wiki.pk]]
    for wiki in moving:
        Wiki.objects.filter(pk=wiki.pk).update(slug=str(wiki.uuid))
    for wiki in moving:
        target = targets[wiki.pk]
        if isinstance(target, tuple):
            base, prefix, preferred = target
            slug = unique_slug(base, is_taken=lambda candidate, pk=wiki.pk: Wiki.objects.filter(slug=candidate).exclude(pk=pk).exists(), prefix=prefix, max_length=_SLUG_MAX_LENGTH, preferred_length=preferred)
            Wiki.objects.filter(pk=wiki.pk).update(slug=slug)


def _lineage(place):
    chain, seen = [], set()
    while place is not None and place.pk not in seen and len(chain) < _MAX_LINEAGE_DEPTH:
        seen.add(place.pk)
        chain.append(place)
        place = place.parent
    return chain


def _descendant_ids(Wiki, wiki):
    found, frontier = {wiki.pk}, [wiki.pk]
    while frontier:
        frontier = [pk for pk in Wiki.objects.filter(parent_wiki_id__in=frontier).values_list("pk", flat=True) if pk not in found]
        found.update(frontier)
    return found


def _nest_root_wikis(Location, Pin, Wiki, WikiEdit):
    """Nest each root wiki on a building location under the nearest wiki its place lineage holds."""
    building_locations = _building_locations(Location, Pin, Wiki).values("pk")
    for wiki in Wiki.objects.filter(parent_wiki__isnull=True, location_id__in=building_locations).select_related("place__parent", "location__place__parent").order_by("pk"):
        own_place = wiki.place
        chain = _lineage(own_place)[1:] if own_place is not None else _lineage(wiki.location.place)
        if not chain:
            continue
        holders = {holder.place_id: holder for holder in Wiki.objects.filter(place_id__in=[place.pk for place in chain]).exclude(pk=wiki.pk)}
        container = next((holders[place.pk] for place in chain if place.pk in holders), None)
        if container is None or container.pk in _descendant_ids(Wiki, wiki):
            continue
        Wiki.objects.filter(pk=wiki.pk).update(parent_wiki=container)
        WikiEdit.objects.create(wiki=container, editor=None, changes={"child_wiki_merged": {"from": None, "to": wiki.name}})


def fix_building_locations(apps, schema_editor):
    """Rename building locations from their own records, re-mint their slugs, drop misplaced CRIS cards, nest their wikis."""
    Location = apps.get_model("dashboard", "Location")
    Pin = apps.get_model("dashboard", "Pin")
    Wiki = apps.get_model("dashboard", "Wiki")
    renamed = _rename_locations(Location, Pin, Wiki, apps.get_model("dashboard", "LocationCache"), apps.get_model("dashboard", "LocationSlugHistory"))
    _nest_root_wikis(Location, Pin, Wiki, apps.get_model("dashboard", "WikiEdit"))
    _remint_wikis(Wiki, renamed)


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0048_user_subscription_granter_kept_revoker_recorded"),
    ]

    operations = [
        migrations.RunPython(fix_building_locations, migrations.RunPython.noop),
    ]
