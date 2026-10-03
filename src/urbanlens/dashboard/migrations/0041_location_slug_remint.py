"""Re-mint Location and Wiki slugs so they come only from a provider's name, or the uuid (P186).

Rows predate ``Location.official_name_source``, so a name counts as a provider's only when stored provider data
holds it: the linked GooglePlace's cached name, or a field a name provider reads from one of the Location's own
cached responses. Anything else may be text a person typed (0003 named Locations from pin names; add-pin and trip
activities seeded the field from the request), so its Location falls back to the uuid. Every readable slug a
Location gives up goes to its slug history, which keeps old links resolving.
"""

import re
import unicodedata

from django.db import migrations

from urbanlens.dashboard.services.core.slugs import PREFERRED_CHILD_SLUG_LENGTH, is_uuid_slug, parent_slug_prefix, unique_slug

_SLUG_MAX_LENGTH = 255


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


def _proven_source(location, location_cache_model):
    """The name source that stored provider data proves ``location.official_name`` came from, else ""."""
    target = _normalized(location.official_name)
    if not target:
        return ""
    google_place = location.google_place
    if google_place is not None and _normalized(google_place.cached_place_name) == target:
        return "google_places"
    for cache_source, data in location_cache_model.objects.filter(location_id=location.pk, source__in=_CACHED_NAMES).order_by("source").values_list("source", "data"):
        name_source, names = _CACHED_NAMES[cache_source]
        if isinstance(data, dict) and any(_normalized(name) == target for name in names(data)):
            return name_source
    return ""


def _fits(slug, base, *, prefix="", preferred_length=_SLUG_MAX_LENGTH):
    """Whether ``slug`` is what minting ``base`` would give, up to a uniqueness suffix."""
    if not slug:
        return False
    ideal = unique_slug(base, is_taken=lambda _candidate: False, prefix=prefix, max_length=_SLUG_MAX_LENGTH, preferred_length=preferred_length)
    return slug == ideal or re.fullmatch(re.escape(ideal) + r"-\d+", slug) is not None


def _remint_locations(Location, LocationCache, LocationSlugHistory):
    def taken(pk):
        return lambda candidate: Location.objects.filter(slug=candidate).exclude(pk=pk).exists() or LocationSlugHistory.objects.filter(slug=candidate).exclude(location_id=pk).exists()

    for location in Location.objects.select_related("google_place").order_by("pk").iterator(chunk_size=500):
        source = _proven_source(location, LocationCache)
        if source and _fits(location.slug, location.official_name):
            new_slug = location.slug
        elif source:
            new_slug = unique_slug(location.official_name, is_taken=taken(location.pk), max_length=_SLUG_MAX_LENGTH)
        else:
            new_slug = location.slug if is_uuid_slug(location.slug) else str(location.uuid)
        if source != location.official_name_source or new_slug != location.slug:
            Location.objects.filter(pk=location.pk).update(official_name_source=source, slug=new_slug)
        if new_slug != location.slug:
            if location.slug and location.slug != str(location.uuid):
                LocationSlugHistory.objects.get_or_create(slug=location.slug, defaults={"location_id": location.pk})
            LocationSlugHistory.objects.filter(location_id=location.pk, slug=new_slug).delete()


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
    for wiki in moving:
        Wiki.objects.filter(pk=wiki["pk"]).update(slug=str(wiki["uuid"]))
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
