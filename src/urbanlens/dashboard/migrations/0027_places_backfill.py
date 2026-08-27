"""Backfill Place from the per-location boundary copies it replaces.

Every Location used to carry its own copy of whatever polygon contained it,
fetched by point lookup. On a property several people pinned - or one where
somebody imported the buildings - that means many identical copies of the same
parcel outline. Clustering those copies back into one row per real-world thing
is the whole job here.

Order matters and is load-bearing:

1. **Parcels** - cluster location-default PROPERTY polygons by mutual centroid
   containment, one Place per cluster.
2. **Buildings** - one Place per distinct BUILDING footprint, ``PART_OF`` the
   parcel that contains it.
3. **Resolve** every Location onto the most specific place containing it.
4. **Wikis** - anchor to the resolved place. Where several wikis land on one
   place, the oldest survives and the rest nest under it; they describe the
   same thing, and nesting (rather than deleting) keeps every article, photo,
   and comment reachable.
5. **Sites** - a wiki whose members ended up spread across several parcels
   becomes a ``SITE`` aggregate over them.
6. **Grants** - snapshot every (profile, wiki) pair visible under the *old*
   rule that the new rule would deny, so the restructure costs nobody access
   they already had. This is why creating sites automatically is safe.

Written against historical models throughout, and idempotent: re-running finds
the places it already made.
"""

from django.db import migrations

#: Mirrors ``services.locations.site_scope.MULTI_BUILDING_THRESHOLD``. Copied
#: rather than imported: a migration must keep behaving the same when the
#: service-layer constant is retuned later.
MULTI_BUILDING_THRESHOLD = 2


def _location_defaults(Boundary, boundary_type):
    """Location-default rows of one type that actually carry geometry."""
    return Boundary.objects.filter(
        pin__isnull=True,
        wiki__isnull=True,
        profile__isnull=True,
        source="",
        location__isnull=False,
        generated_polygon__isnull=False,
        boundary_type=boundary_type,
    ).select_related("location")


def _same_thing(polygon_a, polygon_b):
    """Whether two outlines describe one real-world thing.

    Mutual centroid containment: providers disagree about exact edges
    constantly, but two polygons that each contain the other's centre are the
    same parcel, and two adjacent parcels never are.
    """
    try:
        return polygon_a.contains(polygon_b.centroid) and polygon_b.contains(polygon_a.centroid)
    except Exception:  # noqa: BLE001 - a corrupt legacy polygon must not abort the migration
        return False


def _cluster(rows):
    """Group boundary rows whose polygons describe the same thing.

    Returns:
        List of ``(representative_polygon, [row, ...])`` pairs.
    """
    clusters: list[tuple[object, list]] = []
    for row in rows:
        polygon = row.generated_polygon
        for representative, members in clusters:
            if _same_thing(representative, polygon):
                members.append(row)
                break
        else:
            clusters.append((polygon, [row]))
    return clusters


def _area_sqm(Place, place):
    """Store a place's area, best-effort."""
    from django.contrib.gis.db.models.functions import Area

    try:
        measured = Place.objects.filter(pk=place.pk).annotate(computed_area=Area("geometry")).values_list("computed_area", flat=True).first()
    except Exception:  # noqa: BLE001 - area is an ordering hint, never correctness
        return
    if measured is not None:
        Place.objects.filter(pk=place.pk).update(area_sqm=float(measured.sq_m))


def _create_place(Place, kind, polygon, name=""):
    """Create a place, self-anchoring its access domain."""
    place = Place.objects.create(kind=kind, geometry=polygon, name=(name or "")[:255], geometry_generated_at=None)
    Place.objects.filter(pk=place.pk).update(domain_root=place.pk)
    place.domain_root_id = place.pk
    _area_sqm(Place, place)
    return place


def backfill(apps, schema_editor):  # noqa: PLR0912, PLR0915 - one linear pass; splitting it would hide the ordering the docstring explains
    from django.utils import timezone

    Boundary = apps.get_model("dashboard", "Boundary")
    BoundaryVote = apps.get_model("dashboard", "BoundaryVote")
    Location = apps.get_model("dashboard", "Location")
    Pin = apps.get_model("dashboard", "Pin")
    Place = apps.get_model("dashboard", "Place")
    PlaceAccessGrant = apps.get_model("dashboard", "PlaceAccessGrant")
    Wiki = apps.get_model("dashboard", "Wiki")

    if Place.objects.exists():
        return

    # --- 0. Snapshot who can see what under the OLD rule -------------------
    # Old rule: a profile sees a wiki when it has a pin at that Location, or a
    # pin whose point falls inside that location's own generated polygon.
    old_visible: dict[int, set[int]] = {}
    wiki_locations = list(Wiki.objects.filter(officially_created=True).values_list("location_id", flat=True))
    if wiki_locations:
        pin_points = [(profile_id, point) for profile_id, point in Pin.objects.filter(location__point__isnull=False).values_list("profile_id", "location__point") if point is not None]
        direct = {}
        for profile_id, location_id in Pin.objects.filter(location_id__in=wiki_locations).values_list("profile_id", "location_id"):
            direct.setdefault(location_id, set()).add(profile_id)
        polygons_by_location: dict[int, list] = {}
        for row in _location_defaults(Boundary, "property").filter(location_id__in=wiki_locations):
            polygons_by_location.setdefault(row.location_id, []).append(row.generated_polygon)
        for row in _location_defaults(Boundary, "building").filter(location_id__in=wiki_locations):
            polygons_by_location.setdefault(row.location_id, []).append(row.generated_polygon)
        for location_id in wiki_locations:
            holders = set(direct.get(location_id, ()))
            for polygon in polygons_by_location.get(location_id, ()):
                for profile_id, point in pin_points:
                    if profile_id not in holders and polygon.contains(point):
                        holders.add(profile_id)
            if holders:
                old_visible[location_id] = holders

    # --- 1. Parcels --------------------------------------------------------
    parcel_rows = list(_location_defaults(Boundary, "property"))
    parcel_for_location: dict[int, object] = {}
    parcels: list[object] = []
    for polygon, members in _cluster(parcel_rows):
        name = next((member.location.official_name for member in members if member.location.official_name), "")
        parcel = _create_place(Place, "parcel", polygon, name)
        parcels.append(parcel)
        for member in members:
            parcel_for_location[member.location_id] = parcel

    # --- 2. Buildings ------------------------------------------------------
    building_rows = list(_location_defaults(Boundary, "building"))
    buildings: list[object] = []
    for polygon, members in _cluster(building_rows):
        parcel = next((parcel_for_location.get(member.location_id) for member in members if parcel_for_location.get(member.location_id)), None)
        building = _create_place(Place, "building", polygon, "")
        if parcel is not None:
            Place.objects.filter(pk=building.pk).update(parent=parcel, parent_relation="part_of", domain_root=parcel.domain_root_id)
            building.parent_id, building.domain_root_id = parcel.pk, parcel.domain_root_id
        buildings.append(building)

    for parcel in parcels:
        total = Place.objects.filter(parent=parcel, parent_relation="part_of", kind="building").count()
        Place.objects.filter(pk=parcel.pk).update(building_child_count=total)
        parcel.building_child_count = total

    # --- 3. Resolve locations ---------------------------------------------
    now = timezone.now()
    resolvable = [(place, place.geometry) for place in [*buildings, *parcels] if place.geometry is not None]
    # Buildings first so the most specific match wins, mirroring
    # PlaceQuerySet.containing_point's smallest-area-first ordering.
    for location in Location.objects.filter(point__isnull=False).iterator(chunk_size=500):
        match = next((place for place, geometry in resolvable if geometry.contains(location.point)), None)
        Location.objects.filter(pk=location.pk).update(place=match, place_resolved_at=now)

    # --- 4. Wikis ----------------------------------------------------------
    wiki_by_place: dict[int, object] = {}
    for wiki in Wiki.objects.select_related("location").order_by("created", "pk"):
        place_id = Location.objects.filter(pk=wiki.location_id).values_list("place_id", flat=True).first()
        if place_id is None:
            continue
        survivor = wiki_by_place.get(place_id)
        if survivor is None:
            Wiki.objects.filter(pk=wiki.pk).update(place_id=place_id)
            wiki_by_place[place_id] = wiki
        elif wiki.parent_wiki_id is None and wiki.pk != survivor.pk:
            # Same real-world thing, two pages. Nest rather than delete: every
            # article, photo, and comment stays reachable, and a human can
            # merge the prose later if it is worth merging.
            Wiki.objects.filter(pk=wiki.pk).update(parent_wiki=survivor)

    # --- 5. Sites ----------------------------------------------------------
    # A wiki whose viewers' pins land on several distinct parcels was never
    # about one parcel; it is a site spanning them.
    for location_id, holders in old_visible.items():
        parcel_ids = {
            pid
            for pid in Location.objects.filter(pins__profile_id__in=holders, place__isnull=False).values_list("place__domain_root_id", flat=True).distinct()
            if pid is not None
        }
        wiki_place_id = Location.objects.filter(pk=location_id).values_list("place_id", flat=True).first()
        if wiki_place_id is None or len(parcel_ids) < MULTI_BUILDING_THRESHOLD:
            continue
        members = list(Place.objects.filter(pk__in=parcel_ids, parent__isnull=True).exclude(pk=wiki_place_id))
        if len(members) < MULTI_BUILDING_THRESHOLD:
            continue
        site = _create_place(Place, "site", None, Location.objects.filter(pk=location_id).values_list("official_name", flat=True).first() or "")
        for member in members:
            Place.objects.filter(pk=member.pk).update(parent=site, parent_relation="member_of")
        Place.objects.filter(pk=site.pk).update(is_aggregate=True)

    # --- 6. Votes and candidate boundaries --------------------------------
    for vote in BoundaryVote.objects.all():
        place_id = Location.objects.filter(pk=vote.location_id).values_list("place_id", flat=True).first()
        if place_id is None:
            vote.delete()
            continue
        BoundaryVote.objects.filter(pk=vote.pk).update(place_id=place_id)

    for candidate in Boundary.objects.filter(pin__isnull=True, wiki__isnull=True, profile__isnull=True, location__isnull=False).exclude(source=""):
        place_id = Location.objects.filter(pk=candidate.location_id).values_list("place_id", flat=True).first()
        if place_id is None or Boundary.objects.filter(place_id=place_id, boundary_type=candidate.boundary_type, source=candidate.source).exists():
            candidate.delete()
            continue
        Boundary.objects.filter(pk=candidate.pk).update(place_id=place_id, location=None)

    # --- 7. Grandfather grants --------------------------------------------
    # New rule: a profile sees a wiki when it holds the wiki's access domain.
    granted = 0
    for location_id, holders in old_visible.items():
        wiki_place = Place.objects.filter(locations__id=location_id).first()
        if wiki_place is None:
            continue
        for profile_id in holders:
            reaches = Location.objects.filter(pins__profile_id=profile_id, place__domain_root_id=wiki_place.domain_root_id).exists()
            if reaches:
                continue
            PlaceAccessGrant.objects.get_or_create(profile_id=profile_id, place=wiki_place, defaults={"reason": "backfill"})
            granted += 1

    # --- 8. Retire the per-location copies --------------------------------
    _location_defaults(Boundary, "property").delete()
    _location_defaults(Boundary, "building").delete()
    Boundary.objects.filter(pin__isnull=True, wiki__isnull=True, profile__isnull=True, place__isnull=True, source="").delete()

    # --- 9. Derived marker types ------------------------------------------
    for place in Place.objects.all():
        if place.kind == "site":
            implied = "parcel"
        else:
            parcel = place if place.kind != "building" else (Place.objects.filter(pk=place.parent_id).first() if place.parent_id else None)
            multi = parcel is not None and parcel.building_child_count >= MULTI_BUILDING_THRESHOLD
            implied = ("building" if place.kind == "building" else "parcel") if multi else "location"
        Pin.objects.filter(location__place=place, pin_type_is_user_provided=False).exclude(pin_type=implied).update(pin_type=implied)
        Wiki.objects.filter(place=place, pin_type_is_user_provided=False).exclude(pin_type=implied).update(pin_type=implied)


def unbackfill(apps, schema_editor):
    """Drop places, leaving the columns in place.

    The per-location boundary copies this migration retired are not
    reconstructed - they were duplicates by definition, and the provider chain
    regenerates whatever a location needs on next access.
    """
    apps.get_model("dashboard", "PlaceAccessGrant").objects.all().delete()
    apps.get_model("dashboard", "Location").objects.update(place=None, place_resolved_at=None)
    apps.get_model("dashboard", "Wiki").objects.update(place=None)
    apps.get_model("dashboard", "Place").objects.update(parent=None, domain_root=None)
    apps.get_model("dashboard", "Place").objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0026_places"),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
