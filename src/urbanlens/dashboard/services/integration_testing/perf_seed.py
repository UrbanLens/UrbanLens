"""Seed one account large enough for the neighbour test to mean something.
The performance work this exists for is about what *one* user's account costs everyone else, so its measurements need an account big enough for per-row costs to dominate the constants."""

from __future__ import annotations

from datetime import timedelta
import time
from typing import TYPE_CHECKING, Any, TypeVar

from django.contrib.gis.geos import Point
from django.db import connection, transaction
from django.db.models import Model
from django.utils import timezone

from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile

if TYPE_CHECKING:
    from collections.abc import Sequence

_ModelT = TypeVar("_ModelT", bound=Model)

#: Pins created per `bulk_create` round trip. Large enough that 20,000 rows is a
#: score of statements rather than thousands, small enough that one statement's
BATCH_SIZE = 1_000

#: Degrees between seeded pins, in both axes.
COORDINATE_STEP = 0.01

#: Where the seeded block starts. Mid-Pacific on purpose - far from any real
#: pin, so a seeded account cannot collide with or be mistaken for real data.
ORIGIN_LATITUDE = -30.0
ORIGIN_LONGITUDE = -140.0

#: Pins per row of the grid.
#: A constant, and it has to be: the mapping from index to coordinate must be the same in every run
#: against the same account, or a top-up lays a differently-shaped grid over the first one and
#: collides on the `(latitude, longitude)` unique constraint.
GRID_SIDE = 200

#: Where a seed starts when the caller does not place it.
DEFAULT_ORIGIN = (ORIGIN_LATITUDE, ORIGIN_LONGITUDE)


def max_seeded_pins(origin_latitude: float = ORIGIN_LATITUDE) -> int:
    """Largest seed a grid starting at *origin_latitude* can lay out before running past the pole.

    Args:
        origin_latitude: Latitude of the grid's first row; rows run north.

    Returns:
        The pin count that fills every row up to latitude 90.
    """
    return int(round((90.0 - origin_latitude) / COORDINATE_STEP, 6)) * GRID_SIDE


MAX_SEEDED_PINS = max_seeded_pins()

#: Name of the label every seeded pin carries. One shared label keeps the label-edit fan-out at its heaviest.
HEAVY_LABEL_NAME = "Perf Heavy"

#: Names drawn from when a seed is asked for more than one label per pin. Small
#: and shared, which is what a real account looks like: a vocabulary of a few
#: dozen labels carried by thousands of pins.
VOCABULARY = [
    ("tag", "Abandoned"),
    ("tag", "Industrial"),
    ("tag", "Rooftop"),
    ("tag", "Tunnel"),
    ("tag", "Hospital"),
    ("tag", "Church"),
    ("tag", "School"),
    ("tag", "Rail"),
    ("category", "Factory"),
    ("category", "Residential"),
    ("category", "Military"),
    ("category", "Civic"),
    ("status", "Standing"),
    ("status", "Demolished"),
    ("status", "Sealed"),
    ("status", "Watched"),
]

#: Shared by every seeded pin's name, so a load test can post a filter that
#: genuinely matches all of them. A filter matching nothing measures the query
#: planner rather than the response builder.
PIN_NAME_PREFIX = "Perf Pin"

#: Tables whose planner statistics the seed invalidates.
_ANALYZED_TABLES = ("dashboard_locations", "dashboard_user_pins", "dashboard_labels")


def _grid(index: int, origin: tuple[float, float] = DEFAULT_ORIGIN) -> tuple[str, str]:
    """One pin's coordinates, laid out on a fixed grid.
    A grid rather than a line so a bounding-box query over the seeded block returns a realistic subset rather than everything or nothing.

    Args:
        index: Which pin, zero-based.
        origin: ``(latitude, longitude)`` of pin zero.

    Returns:
        ``(latitude, longitude)`` as strings, since the columns are decimals and a float would round differently on the way in."""
    latitude = origin[0] + (index // GRID_SIDE) * COORDINATE_STEP
    longitude = origin[1] + (index % GRID_SIDE) * COORDINATE_STEP
    return f"{latitude:.6f}", f"{longitude:.6f}"


def _precompute_map_center(profile: Profile, total: int, origin: tuple[float, float] = DEFAULT_ORIGIN) -> tuple[float, float] | None:
    """Store the account's map centre without computing it the expensive way.

    Args:
        profile: The seeded account.
        total: How many pins it now has.
        origin: Where its grid starts.

    Returns:
        The stored ``(latitude, longitude)``, or None when there was nothing to average."""
    if total <= 0:
        return None
    points = [_grid(index, origin) for index in range(total)]
    latitude = sum(float(lat) for lat, _ in points) / total
    longitude = sum(float(lng) for _, lng in points) / total
    Profile.objects.filter(pk=profile.pk).update(map_center_latitude=latitude, map_center_longitude=longitude)
    profile.map_center_latitude = latitude
    profile.map_center_longitude = longitude
    return latitude, longitude


def seed_heavy_account(
    profile: Profile,
    *,
    pins: int,
    analyze: bool = True,
    precompute_map_center: bool = True,
    labels_per_pin: int = 1,
    origin: tuple[float, float] = DEFAULT_ORIGIN,
) -> dict[str, Any]:
    """Give *profile* *pins* root pins, all carrying one shared label.

    Args:
        profile: The account to seed.
        pins: How many root pins it should end up with.
        analyze: Refresh planner statistics afterwards.
        labels_per_pin: How many labels each pin carries.
        precompute_map_center: Store the map centre directly instead of leaving the first page load to derive it.
        origin: ``(latitude, longitude)`` of the account's first pin. Two accounts whose grids overlap share the
            places under the overlap, as two real users pinning the same building do.

    Raises:
        ValueError: ``pins`` is larger than the grid can lay out from ``origin``, or the grid would cross the antimeridian.

    Returns:
        What was done, for the provisioning manifest: the final pin count, how many were created now, the shared label's id and name, whether `ANALYZE` ran, and how long it took."""
    ceiling = max_seeded_pins(origin[0])
    if pins > ceiling:
        raise ValueError(f"{pins} pins would run the grid past the north pole; a grid starting at latitude {origin[0]} tops out at {ceiling}.")
    if not -180.0 <= origin[1] <= 180.0 - (GRID_SIDE - 1) * COORDINATE_STEP:
        raise ValueError(f"A grid starting at longitude {origin[1]} would cross the antimeridian.")
    # One short of the vocabulary, so the window can rotate: a pin that took every
    # label would make every pin identical, which is the case this exists to avoid.
    if labels_per_pin < 1 or labels_per_pin > len(VOCABULARY):
        raise ValueError(f"labels_per_pin must be between 1 and {len(VOCABULARY)}; got {labels_per_pin}.")

    started = time.perf_counter()
    existing = Pin.objects.filter(profile=profile).root_pins().count()
    wanted = max(pins - existing, 0)

    label, _ = Label.objects.get_or_create(
        name=HEAVY_LABEL_NAME,
        kind="tag",
        profile=profile,
        defaults={"color": "#b34747", "description": "Every pin in a seeded performance account carries this."},
    )

    # The whole vocabulary whenever more than the shared label is wanted: each pin
    # takes a window of it, so it has to be larger than the window or every pin
    # ends up with the same set and the fixture stops resembling an account.
    extras = _vocabulary_labels(profile) if labels_per_pin > 1 else []
    per_pin = labels_per_pin - 1

    created = 0
    for start in range(0, wanted, BATCH_SIZE):
        count = min(BATCH_SIZE, wanted - start)
        with transaction.atomic():
            coordinates = [_grid(existing + start + offset, origin) for offset in range(count)]
            locations = _locations_for(coordinates, first_index=existing + start)
            seeded_pins = Pin.objects.bulk_create(
                [
                    Pin(
                        profile=profile,
                        location=location,
                        name=f"{PIN_NAME_PREFIX} {existing + start + offset}",
                        # Set explicitly because `bulk_create` does not call `save`, which is where
                        # `ensure_slug` runs.
                        # A null slug is legal - the payload falls back to the uuid - but it would
                        # put every seeded pin on a code path real pins do not take, which is the
                        slug=f"perf-pin-{existing + start + offset}",
                    )
                    for offset, location in enumerate(locations)
                ],
            )
            through = Pin.labels.through
            pairs = []
            for offset, pin in enumerate(seeded_pins):
                pairs.append(through(pin_id=pin.pk, label_id=label.pk))
                index = existing + start + offset
                # A rotating window rather than a random sample: the distribution
                # is the same on every run, so two measurements are comparable.
                pairs.extend(through(pin_id=pin.pk, label_id=extras[(index + step) % len(extras)].pk) for step in range(per_pin))
            through.objects.bulk_create(pairs)
        created += len(seeded_pins)

    analyzed = analyze and analyze_seeded_tables()
    centre = _precompute_map_center(profile, existing + created, origin) if precompute_map_center else None
    return {
        "map_center": list(centre) if centre else None,
        "pins": existing + created,
        "created": created,
        "already_present": existing,
        "label": label.name,
        "label_id": label.pk,
        "label_kind": label.kind,
        "labels_per_pin": labels_per_pin,
        "pin_name_prefix": PIN_NAME_PREFIX,
        "analyzed": analyzed,
        "seconds": round(time.perf_counter() - started, 1),
    }


#: Name prefix for labels seeded purely to grow the labels table (P123). Unattached to any
#: pin/image/wiki on purpose: the semi-join this reproduces (`services/global_search/providers.py`'s
#: `_semijoin`) scans the whole `dashboard_labels` table before any join to another model narrows
#: it, so what these rows are attached to has no bearing on the cost - only how many exist does.
BULK_LABEL_PREFIX = "Perf Bulk Label"


def seed_bulk_labels(profile: Profile, *, count: int, analyze: bool = True, batch_size: int = BATCH_SIZE) -> dict[str, Any]:
    """Grow `dashboard_labels` by *count* rows, for P123's cross-account label-scan load test.

    Args:
        profile: Whose account the rows are created under. Immaterial to the defect being
            reproduced - the scan is unscoped by profile - but a label row requires one.
        count: How many bulk labels should exist in total. Tops up rather than restarting:
            existing bulk labels (by name prefix) are counted first and only the shortfall is
            created, the same convention `seed_heavy_account` uses for pins.
        analyze: Refresh planner statistics on `dashboard_labels` afterwards.
        batch_size: Rows per `bulk_create` round trip.

    Returns:
        What was done, for the provisioning manifest: the final count, how many were created now,
        whether `ANALYZE` ran, and how long it took."""
    started = time.perf_counter()
    existing = Label.objects.filter(profile=profile, name__startswith=BULK_LABEL_PREFIX).count()
    wanted = max(count - existing, 0)

    created = 0
    for start in range(0, wanted, batch_size):
        batch = min(batch_size, wanted - start)
        Label.objects.bulk_create(
            [Label(profile=profile, kind="tag", name=f"{BULK_LABEL_PREFIX} {existing + start + offset}", color="#4a6fa5") for offset in range(batch)],
        )
        created += batch

    analyzed = analyze and analyze_seeded_tables(("dashboard_labels",))
    return {
        "labels": existing + created,
        "created": created,
        "already_present": existing,
        "name_prefix": BULK_LABEL_PREFIX,
        "analyzed": analyzed,
        "seconds": round(time.perf_counter() - started, 1),
    }


#: Name prefix shared by every row `seed_bulk_search_relations` creates. Each relation is attached
#: to one dedicated host row (a pin, a wiki, a trip, a check-in) rather than left unattached like
#: `BULK_LABEL_PREFIX` - a NOT NULL foreign key requires a host - but the host is otherwise
#: irrelevant to the defect: like the labels semi-join, `_semijoin` scans the whole relation table
#: before any join to the search account's own rows narrows it, so which host the rows are attached
#: to has no bearing on the cost.
BULK_RELATIONS_PREFIX = "Perf Bulk Relation"

#: Host object names/titles - deliberately a different string from `BULK_RELATIONS_PREFIX`. A pin's
#: own name is auto-aliased on save (`Pin.save`'s alias-history sync), so naming the host pin with
#: the bulk prefix itself would make `_top_up`'s `name__startswith` count that one alias as already
#: present on every fresh run - harmless to the defect (it is one more row in a table this measures
#: by total size anyway) but a confusing off-by-one in the report.
_HOST_NAME_PREFIX = "Perf Search Relations Host"

#: (host attribute on the seeded account, db_table) pairs `seed_bulk_search_relations` grows -
#: one host row apiece, `count` children apiece. Mirrors docs/archive/PROBLEMS-ARCHIVE.md's
#: (formerly P123) entry on ArticleSearchProvider/TripSearchProvider/SafetySearchProvider's own
#: to-many-crossing paths.
_RELATION_TABLES = (
    "dashboard_pin_aliases",
    "dashboard_wiki_aliases",
    "dashboard_trip_activities",
    "dashboard_trip_comments",
    "dashboard_safety_checkin_messages",
)


def _bulk_relation_host_location(profile: Profile) -> Location:
    """The `Location` that seeds *profile*'s `seed_bulk_search_relations` host rows.

    `Wiki` carries no `profile` field of its own - it is a global page keyed by `location` alone -
    so this location has to be unique per profile, not shared the way `seed_bulk_labels`' unattached
    rows are: two profiles sharing one location would share one `host_wiki`, and each profile's
    `WikiAlias` top-up would count the other's rows as already present.

    Args:
        profile: The account whose host location this is.

    Returns:
        The location, created on first use."""
    longitude = 179.0 - (profile.pk % 900_000) * 0.000_001
    location, _ = Location.objects.get_or_create(
        latitude="-89.000000",
        longitude=f"{longitude:.6f}",
        defaults={"official_name": f"Perf Search Relations Host ({profile.pk})", "point": Point(longitude, -89.0, srid=4326)},
    )
    return location


def seed_bulk_search_relations(profile: Profile, *, count: int, analyze: bool = True, batch_size: int = BATCH_SIZE) -> dict[str, Any]:
    """Grow the five to-many relations P123 generalised to, for the same cross-account scan load test.

    One dedicated host row per relation (a pin, its wiki, a trip, a check-in) carries *count* children
    each: `PinAlias`/`WikiAlias` (`ArticleSearchProvider`'s `pin__aliases__name`/`wiki__aliases__name`),
    `TripActivity`/`TripComment` (`TripSearchProvider`'s `activities__title`/`activities__notes`/
    `comments__text`), and `SafetyCheckinMessage` (`SafetySearchProvider`'s `messages__body`). See
    `docs/archive/PROBLEMS-ARCHIVE.md`'s (formerly P123) entry for the mechanism each shares with the
    original label semi-join.

    Args:
        profile: Whose account the host rows are created under. Immaterial to the defect being
            reproduced - every semi-join here is unscoped - but each relation's foreign key is
            NOT NULL, so a host is required.
        count: How many bulk children each relation should end up with. Tops up rather than
            restarting: existing bulk rows (by name/text/title prefix) are counted first and only
            the shortfall is created, the same convention `seed_bulk_labels` uses.
        analyze: Refresh planner statistics on all five tables afterwards.
        batch_size: Rows per `bulk_create` round trip, per relation.

    Returns:
        What was done, for the provisioning manifest: one sub-report per relation plus whether
        `ANALYZE` ran and how long the whole thing took."""
    from urbanlens.dashboard.models.aliases.model import PinAlias, WikiAlias
    from urbanlens.dashboard.models.safety.model import SafetyCheckin, SafetyCheckinMessage
    from urbanlens.dashboard.models.trips.model import Trip, TripActivity, TripComment
    from urbanlens.dashboard.models.wiki.model import Wiki

    started = time.perf_counter()
    location = _bulk_relation_host_location(profile)

    host_pin, _ = Pin.objects.get_or_create(
        profile=profile,
        location=location,
        defaults={"name": f"{_HOST_NAME_PREFIX} Pin", "slug": "perf-search-relations-host-pin"},
    )
    host_wiki, _ = Wiki.objects.get_or_create(location=location, defaults={"name": f"{_HOST_NAME_PREFIX} Wiki"})
    host_trip, _ = Trip.objects.get_or_create(creator=profile, name=f"{_HOST_NAME_PREFIX} Trip")
    host_checkin, _ = SafetyCheckin.objects.get_or_create(
        profile=profile,
        title=f"{_HOST_NAME_PREFIX} Checkin",
        defaults={"checkin_by": timezone.now() + timedelta(days=3650)},
    )

    def _top_up(model: type[_ModelT], host_field: str, host: Model, text_field: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        existing = model._default_manager.filter(**{host_field: host, f"{text_field}__startswith": BULK_RELATIONS_PREFIX}).count()  # noqa: SLF001
        wanted = max(count - existing, 0)
        created = 0
        for start in range(0, wanted, batch_size):
            batch = min(batch_size, wanted - start)
            rows: list[_ModelT] = []
            for offset in range(batch):
                fields: dict[str, Any] = {host_field: host, text_field: f"{BULK_RELATIONS_PREFIX} {existing + start + offset}"}
                fields.update(extra or {})
                rows.append(model(**fields))
            model._default_manager.bulk_create(rows)  # noqa: SLF001
            created += batch
        return {"count": existing + created, "created": created, "already_present": existing}

    report: dict[str, Any] = {
        "pin_aliases": _top_up(PinAlias, "pin", host_pin, "name"),
        "wiki_aliases": _top_up(WikiAlias, "wiki", host_wiki, "name"),
        "trip_activities": _top_up(TripActivity, "trip", host_trip, "title", extra={"notes": ""}),
        "trip_comments": _top_up(TripComment, "trip", host_trip, "text"),
        "safety_messages": _top_up(SafetyCheckinMessage, "checkin", host_checkin, "body"),
    }
    report["analyzed"] = analyze and analyze_seeded_tables(_RELATION_TABLES)
    report["name_prefix"] = BULK_RELATIONS_PREFIX
    report["seconds"] = round(time.perf_counter() - started, 1)
    return report


def _vocabulary_labels(profile: Profile) -> list[Label]:
    """The shared vocabulary, created if absent.

    Args:
        profile: The account the labels belong to.

    Returns:
        The labels, in `VOCABULARY` order.
    """
    labels = []
    for kind, name in VOCABULARY:
        label, _ = Label.objects.get_or_create(name=name, kind=kind, profile=profile, defaults={"color": "#4a6fa5"})
        labels.append(label)
    return labels


def _locations_for(coordinates: list[tuple[str, str]], *, first_index: int) -> list[Location]:
    """The `Location` rows for *coordinates*, creating only the missing ones.

    Args:
        coordinates: ``(latitude, longitude)`` string pairs, in pin order.
        first_index: Index of the first coordinate, for naming new rows.

    Returns:
        One `Location` per coordinate, in the same order.

    Raises:
        RuntimeError: A coordinate was neither found nor created, which would mean the grid produced a value the database rounded differently - silently pairing pins with the wrong places."""
    Location.objects.bulk_create(
        [Location(latitude=lat, longitude=lng, point=Point(float(lng), float(lat), srid=4326), official_name=f"Perf Place {first_index + offset}") for offset, (lat, lng) in enumerate(coordinates)],
        ignore_conflicts=True,
    )
    # Re-read rather than trusting `bulk_create`'s return: with
    # `ignore_conflicts` it does not set primary keys, and the rows that already
    # existed are not in it at all.
    latitudes = {lat for lat, _ in coordinates}
    longitudes = {lng for _, lng in coordinates}
    found = {(f"{row.latitude:f}", f"{row.longitude:f}"): row for row in Location.objects.filter(latitude__in=latitudes, longitude__in=longitudes)}
    try:
        return [found[coordinate] for coordinate in coordinates]
    except KeyError as error:
        raise RuntimeError(f"No Location at {error.args[0]} after creating it; the grid and the column's rounding disagree.") from error


def analyze_seeded_tables(tables: Sequence[str] = _ANALYZED_TABLES) -> bool:
    """Refresh planner statistics for the tables a seed wrote to.

    Only the tables' owner may, and Postgres skips anyone else with a warning rather than an error, so a
    per-tier login role would otherwise report a refresh that never happened.

    Args:
        tables: Table names, as the models declare them.

    Returns:
        Whether it ran."""
    if connection.vendor != "postgresql":
        return False
    with connection.cursor() as cursor:
        cursor.execute("SELECT bool_and(pg_has_role(relowner, 'USAGE')) FROM pg_class WHERE oid = ANY(%s::regclass[])", [list(tables)])
        if cursor.fetchone() != (True,):
            return False
        cursor.execute("ANALYZE " + ", ".join(connection.ops.quote_name(table) for table in tables))
    return True
