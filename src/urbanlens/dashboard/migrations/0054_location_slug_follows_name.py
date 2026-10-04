"""Re-mint every Location and Wiki slug its provider name could no longer have given (P250).

A provider rename or a cleared name now re-mints a Location's slug as it happens (``Location._sync_slug_after_save``);
rows renamed after 0041 ran still carry their old name's slug. As in 0041, a slug with no provider name behind it
falls back to the uuid, and every readable slug given up goes to the slug history. A Location takes back a former slug
of its own that its name could have given before minting another, as the runtime does.
"""

from collections import defaultdict

from django.db import migrations

from urbanlens.dashboard.services.core.slugs import PREFERRED_CHILD_SLUG_LENGTH, could_mint, is_uuid_slug, parent_slug_prefix, unique_slug

_SLUG_MAX_LENGTH = 255
_CHUNK_SIZE = 500
#: ``slug_history.MAX_FORMER_SLUGS`` when this migration was written.
_MAX_FORMER_SLUGS = 10


def _provider_name(name, source):
    return name if name and source else None


def _chunks(queryset):
    chunk = []
    for row in queryset.iterator(chunk_size=_CHUNK_SIZE):
        chunk.append(row)
        if len(chunk) == _CHUNK_SIZE:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def _fits(slug, name, *, prefix="", preferred_length=_SLUG_MAX_LENGTH):
    return could_mint(slug or "", name, prefix=prefix, max_length=_SLUG_MAX_LENGTH, preferred_length=preferred_length)


def _remint_locations(Location, LocationSlugHistory):
    def taken(pk, candidate):
        return Location.objects.filter(slug=candidate).exclude(pk=pk).exists() or LocationSlugHistory.objects.filter(slug=candidate).exclude(location_id=pk).exists()

    rows = Location.objects.order_by("pk").only("pk", "uuid", "slug", "official_name", "official_name_source")
    for chunk in _chunks(rows):
        stale = []
        for location in chunk:
            name = _provider_name(location.official_name, location.official_name_source)
            if not (_fits(location.slug, name) if name else is_uuid_slug(location.slug)):
                stale.append((location, name))
        if not stale:
            continue
        formers = defaultdict(list)
        for location_id, slug in LocationSlugHistory.objects.filter(location_id__in=[location.pk for location, _name in stale]).order_by("-pk").values_list("location_id", "slug"):
            formers[location_id].append(slug)

        for location, name in stale:
            former = location.slug
            if not name:
                slug = str(location.uuid)
            else:
                slug = next((own for own in formers[location.pk] if _fits(own, name) and not taken(location.pk, own)), None)
                slug = slug or unique_slug(name, is_taken=lambda candidate, pk=location.pk: taken(pk, candidate), max_length=_SLUG_MAX_LENGTH)
            Location.objects.filter(pk=location.pk).update(slug=slug)
            LocationSlugHistory.objects.filter(location_id=location.pk, slug=slug).delete()
            if former and former != slug and former != str(location.uuid):
                LocationSlugHistory.objects.get_or_create(slug=former, defaults={"location_id": location.pk})
                oldest = LocationSlugHistory.objects.filter(location_id=location.pk).order_by("-pk").values_list("pk", flat=True)[_MAX_FORMER_SLUGS:]
                LocationSlugHistory.objects.filter(pk__in=list(oldest)).delete()


def _remint_wikis(Wiki, Location):
    names = {
        pk: _provider_name(name, source)
        for pk, name, source in Location.objects.exclude(official_name_source="").exclude(official_name__isnull=True).exclude(official_name="").values_list("pk", "official_name", "official_name_source")
    }
    wikis = list(Wiki.objects.order_by("pk").values("pk", "uuid", "slug", "location_id", "parent_wiki_id"))
    location_of = {wiki["pk"]: wiki["location_id"] for wiki in wikis}

    targets = {}
    for wiki in wikis:
        base = names.get(wiki["location_id"])
        if not base:
            if wiki["slug"] != str(wiki["uuid"]):
                targets[wiki["pk"]] = None
            continue
        parent_name = names.get(location_of.get(wiki["parent_wiki_id"])) if wiki["parent_wiki_id"] else None
        prefix = parent_slug_prefix([parent_name]) if parent_name else ""
        preferred = PREFERRED_CHILD_SLUG_LENGTH if wiki["parent_wiki_id"] else _SLUG_MAX_LENGTH
        if not _fits(wiki["slug"], base, prefix=prefix, preferred_length=preferred):
            targets[wiki["pk"]] = (base, prefix, preferred)

    # Every wiki that moves first steps onto its uuid, so one's stale slug never keeps another off the slug it should have.
    moving = [wiki for wiki in wikis if wiki["pk"] in targets]
    Wiki.objects.bulk_update([Wiki(pk=wiki["pk"], slug=str(wiki["uuid"])) for wiki in moving], ["slug"], batch_size=_CHUNK_SIZE)
    for wiki in moving:
        target = targets[wiki["pk"]]
        if target is not None:
            base, prefix, preferred = target
            slug = unique_slug(base, is_taken=lambda candidate, pk=wiki["pk"]: Wiki.objects.filter(slug=candidate).exclude(pk=pk).exists(), prefix=prefix, max_length=_SLUG_MAX_LENGTH, preferred_length=preferred)
            Wiki.objects.filter(pk=wiki["pk"]).update(slug=slug)


def remint_mismatched_slugs(apps, schema_editor):
    """Re-mint each Location's and Wiki's slug that its provider name could no longer have given."""
    Location = apps.get_model("dashboard", "Location")
    _remint_locations(Location, apps.get_model("dashboard", "LocationSlugHistory"))
    _remint_wikis(apps.get_model("dashboard", "Wiki"), Location)


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0053_notification_fold"),
    ]

    operations = [
        migrations.RunPython(remint_mismatched_slugs, migrations.RunPython.noop),
    ]
