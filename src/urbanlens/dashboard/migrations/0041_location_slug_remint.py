"""Re-mint Location and Wiki slugs so they come only from a provider's name, or the uuid (P186).

Rows predate ``Location.official_name_source``, so a name counts as a provider's only when stored provider data
holds it: the linked GooglePlace's cached name, or a field a name provider reads from one of the Location's own
cached responses. Anything else may be text a person typed (0003 named Locations from pin names; add-pin and trip
activities seeded the field from the request), so its Location falls back to the uuid. Every readable slug a
Location gives up goes to its slug history, which keeps old links resolving.
"""

from collections import defaultdict
import unicodedata

from django.db import migrations

from urbanlens.dashboard.services.core.slugs import PREFERRED_CHILD_SLUG_LENGTH, could_mint, is_uuid_slug, parent_slug_prefix, unique_slug

_SLUG_MAX_LENGTH = 255
_CHUNK_SIZE = 500


def _strings(value):
    return [value] if isinstance(value, str) else []


def _field(data, *path):
    for key in path:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _nominatim(data):
    old = data.get("old_name")
    return [*_strings(data.get("name")), *([part.strip() for part in old.split(";")] if isinstance(old, str) else [])]


def _historic_registers(data):
    rows = data.get("resources")
    return [row["name"] for row in rows or [] if isinstance(row, dict) and row.get("contains_point") is True and isinstance(row.get("name"), str)]


def _cris(data):
    district = data.get("district")
    names = _strings(data.get("USNName"))
    if isinstance(district, dict) and district.get("contains_point") is True:
        names += [district[key] for key in ("HistoricName", "DistrictName", "USNName", "Name") if isinstance(district.get(key), str)]
    return names


#: LocationCache source -> (the name source naming records for it, the names its provider reads from the payload).
_CACHED_NAMES = {
    "google_places": ("google_places", lambda data: _strings(data.get("name"))),
    "nominatim": ("nominatim", _nominatim),
    "wikipedia": ("wikipedia", lambda data: _strings(data.get("title"))),
    "redata_building_attributes": ("redata_building", lambda data: _strings(data.get("name"))),
    "azure_maps": ("azure_maps", lambda data: [*_strings(_field(data, "poi", "name")), *_strings(data.get("name"))]),
    "epa_echo": ("epa_echo", lambda data: _strings(_field(data, "exact_site", "name"))),
    "cris_building_usn": ("cris", _cris),
    "nps": ("nps", lambda data: _strings(data.get("full_name"))),
    "redata_historic_registers": ("historic_register", _historic_registers),
}


def _normalized(name):
    folded = unicodedata.normalize("NFKC", name or "").casefold()
    return "".join(char for char in folded if char.isalnum())


def _proven_source(location, cached):
    """The name source that stored provider data proves ``location.official_name`` came from, else "".

    Args:
        location: The Location, with its GooglePlace selected.
        cached: Its ``(cache source, data)`` rows for the sources in ``_CACHED_NAMES``, ordered by source.
    """
    target = _normalized(location.official_name)
    if not target:
        return ""
    google_place = location.google_place
    if google_place is not None and _normalized(google_place.cached_place_name) == target:
        return "google_places"
    for cache_source, data in cached:
        name_source, names = _CACHED_NAMES[cache_source]
        if isinstance(data, dict) and any(_normalized(name) == target for name in names(data)):
            return name_source
    return ""


def _chunks(queryset, size):
    chunk = []
    for row in queryset.iterator(chunk_size=size):
        chunk.append(row)
        if len(chunk) == size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def _fits(slug, base, *, prefix="", preferred_length=_SLUG_MAX_LENGTH):
    """Whether minting ``base`` could have given ``slug``, whichever of its candidates were taken at the time."""
    return could_mint(slug or "", base, prefix=prefix, max_length=_SLUG_MAX_LENGTH, preferred_length=preferred_length)


def _remint_locations(Location, LocationCache, LocationSlugHistory):
    """Decide each Location's source and slug a chunk at a time: one cache read and bulk writes per chunk.

    A slug minted earlier in the same chunk is not written yet, so ``claimed`` keeps a second Location off it.
    """
    for chunk in _chunks(Location.objects.select_related("google_place").order_by("pk"), _CHUNK_SIZE):
        named = [location.pk for location in chunk if _normalized(location.official_name)]
        cached = defaultdict(list)
        rows = LocationCache.objects.filter(location_id__in=named, source__in=_CACHED_NAMES).order_by("location_id", "source").values_list("location_id", "source", "data")
        for location_id, cache_source, data in rows.iterator(chunk_size=_CHUNK_SIZE):
            cached[location_id].append((cache_source, data))

        claimed = set()

        def taken(pk, candidate):
            return candidate in claimed or Location.objects.filter(slug=candidate).exclude(pk=pk).exists() or LocationSlugHistory.objects.filter(slug=candidate).exclude(location_id=pk).exists()

        changed, former, reclaimed = [], [], []
        for location in chunk:
            source = _proven_source(location, cached.get(location.pk, ()))
            if source and _fits(location.slug, location.official_name):
                new_slug = location.slug
            elif source:
                new_slug = unique_slug(location.official_name, is_taken=lambda candidate, pk=location.pk: taken(pk, candidate), max_length=_SLUG_MAX_LENGTH)
                claimed.add(new_slug)
            else:
                new_slug = location.slug if is_uuid_slug(location.slug) else str(location.uuid)
            if new_slug != location.slug:
                if location.slug and location.slug != str(location.uuid):
                    former.append(LocationSlugHistory(slug=location.slug, location_id=location.pk))
                reclaimed.append((location.pk, new_slug))
            if source != location.official_name_source or new_slug != location.slug:
                location.official_name_source = source
                location.slug = new_slug
                changed.append(location)

        Location.objects.bulk_update(changed, ["official_name_source", "slug"], batch_size=_CHUNK_SIZE)
        if reclaimed:
            # A slug a Location takes back is no longer a former one of its own.
            ids = [pk for pk, _slug in reclaimed]
            mine = set(reclaimed)
            stale = [pk for pk, location_id, slug in LocationSlugHistory.objects.filter(location_id__in=ids, slug__in=[slug for _pk, slug in reclaimed]).values_list("pk", "location_id", "slug") if (location_id, slug) in mine]
            LocationSlugHistory.objects.filter(pk__in=stale).delete()
        LocationSlugHistory.objects.bulk_create(former, ignore_conflicts=True, batch_size=_CHUNK_SIZE)


def _remint_wikis(Wiki, Location):
    provider_names = dict(Location.objects.exclude(official_name_source="").exclude(official_name__isnull=True).exclude(official_name="").values_list("pk", "official_name"))
    wikis = list(Wiki.objects.order_by("pk").values("pk", "uuid", "slug", "location_id", "parent_wiki_id"))
    location_of = {wiki["pk"]: wiki["location_id"] for wiki in wikis}

    targets = {}
    for wiki in wikis:
        base = provider_names.get(wiki["location_id"])
        if not base:
            targets[wiki["pk"]] = str(wiki["uuid"])
            continue
        parent_name = provider_names.get(location_of.get(wiki["parent_wiki_id"])) if wiki["parent_wiki_id"] else None
        prefix = parent_slug_prefix([parent_name]) if parent_name else ""
        preferred = PREFERRED_CHILD_SLUG_LENGTH if wiki["parent_wiki_id"] else _SLUG_MAX_LENGTH
        targets[wiki["pk"]] = None if _fits(wiki["slug"], base, prefix=prefix, preferred_length=preferred) else (base, prefix, preferred)

    # Every wiki that moves first steps onto its uuid, so one wiki's old community slug never blocks another's new one.
    moving = [wiki for wiki in wikis if targets[wiki["pk"]] is not None and wiki["slug"] != targets[wiki["pk"]]]
    Wiki.objects.bulk_update([Wiki(pk=wiki["pk"], slug=str(wiki["uuid"])) for wiki in moving], ["slug"], batch_size=_CHUNK_SIZE)
    for wiki in moving:
        target = targets[wiki["pk"]]
        if isinstance(target, tuple):
            base, prefix, preferred = target
            slug = unique_slug(base, is_taken=lambda candidate, pk=wiki["pk"]: Wiki.objects.filter(slug=candidate).exclude(pk=pk).exists(), prefix=prefix, max_length=_SLUG_MAX_LENGTH, preferred_length=preferred)
            Wiki.objects.filter(pk=wiki["pk"]).update(slug=slug)


def remint_location_slugs(apps, schema_editor):
    """Record each Location's provable name source, then re-mint every Location and Wiki slug from it."""
    Location = apps.get_model("dashboard", "Location")
    _remint_locations(Location, apps.get_model("dashboard", "LocationCache"), apps.get_model("dashboard", "LocationSlugHistory"))
    _remint_wikis(apps.get_model("dashboard", "Wiki"), Location)


class Migration(migrations.Migration):

    dependencies = [
        ('dashboard', '0040_location_official_name_source'),
    ]

    operations = [
        migrations.RunPython(remint_location_slugs, migrations.RunPython.noop),
    ]
