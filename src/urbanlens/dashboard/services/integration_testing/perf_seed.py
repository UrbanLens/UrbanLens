"""Seed one account large enough for the neighbour test to mean something.

The performance work this exists for is about what *one* user's account costs
everyone else, so its measurements need an account big enough for per-row costs
to dominate the constants. Twenty thousand pins is the working size: R27
measured the map payload at 10,000 (5.79s wall before the fix, 88% of it Python
object construction), and the endpoints still on the model path scale from there.

Three things this does that a loop of `baker.make` would not, each of which was
a real defect in something before it was one here:

- **It runs `ANALYZE` afterwards.** Seeding leaves `pg_class.reltuples`
  describing an empty table, so the planner chooses for a table that no longer
  exists. Measured at 5,000 pins: 4.683s without, 0.384s with (N10). A seeded
  benchmark that skips this measures the planner's ignorance and reads exactly
  like a regression.
- **It spreads coordinates widely.** `Location` is unique on
  `(latitude, longitude)`, and beyond the constraint the importer treats points
  ~11m apart as the same place - three pins at 0.0001 degrees created one row
  when this was checked. Rows that silently merge make a seed size a lie.
- **It gives every pin the same label.** That is the P102 trigger: the
  label-edit fan-out is proportional to the pins carrying the label, so a seed
  that spreads labels evenly across pins would make the heaviest action in the
  neighbour scenario look cheap.

Deliberately `bulk_create` rather than `Model.save`: signals on `Pin` enqueue
cache work and wiki creation per row, and seeding is not the thing under test.
The consequence is that seeded rows have not been through those signals, nor
through `save`, so anything `save` computes is set here explicitly or is absent.
That is correct for a load fixture and wrong for a correctness fixture - do not
reuse this to test behaviour that a signal or `save` produces.
"""

from __future__ import annotations

import time
from typing import Any

from django.db import connection, transaction

from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile

#: Pins created per `bulk_create` round trip. Large enough that 20,000 rows is a
#: score of statements rather than thousands, small enough that one statement's
#: parameter list stays well inside Postgres' 65,535 bound.
BATCH_SIZE = 1_000

#: Degrees between seeded pins, in both axes. Chosen against the importer's own
#: matching behaviour rather than against the unique constraint: 0.0001 degrees
#: (~11m) is unique but is treated as the same place, so a seed at that spacing
#: reports a row count it did not create.
COORDINATE_STEP = 0.01

#: Where the seeded block starts. Mid-Pacific on purpose - far from any real
#: pin, so a seeded account cannot collide with or be mistaken for real data.
ORIGIN_LATITUDE = -30.0
ORIGIN_LONGITUDE = -140.0

#: Pins per row of the grid. A constant, and it has to be: the mapping from
#: index to coordinate must be the same in every run against the same account,
#: or a top-up lays a differently-shaped grid over the first one and collides on
#: the `(latitude, longitude)` unique constraint. Deriving it from the run's own
#: batch size looked reasonable and failed the first time a top-up asked for a
#: different number than the run before it.
GRID_SIDE = 200

#: Largest seed this coordinate scheme can lay out without leaving valid
#: latitudes. `GRID_SIDE` columns per row, `COORDINATE_STEP` degrees per row,
#: starting at `ORIGIN_LATITUDE` and running north.
MAX_SEEDED_PINS = int((90.0 - ORIGIN_LATITUDE) / COORDINATE_STEP) * GRID_SIDE

#: Name of the label every seeded pin carries. One shared label is what makes a
#: label edit expensive; see this module's docstring.
HEAVY_LABEL_NAME = "Perf Heavy"

#: Shared by every seeded pin's name, so a load test can post a filter that
#: genuinely matches all of them. A filter matching nothing measures the query
#: planner rather than the response builder.
PIN_NAME_PREFIX = "Perf Pin"

#: Tables whose planner statistics the seed invalidates.
_ANALYZED_TABLES = ("dashboard_locations", "dashboard_user_pins", "dashboard_labels")


def _grid(index: int) -> tuple[str, str]:
    """One pin's coordinates, laid out on a fixed grid.

    A grid rather than a line so a bounding-box query over the seeded block
    returns a realistic subset rather than everything or nothing. A *fixed*
    grid because this must be a pure function of ``index`` and nothing else -
    two runs against the same account have to agree about where pin 8,000 goes.

    Args:
        index: Which pin, zero-based.

    Returns:
        ``(latitude, longitude)`` as strings, since the columns are decimals and
        a float would round differently on the way in.
    """
    latitude = ORIGIN_LATITUDE + (index // GRID_SIDE) * COORDINATE_STEP
    longitude = ORIGIN_LONGITUDE + (index % GRID_SIDE) * COORDINATE_STEP
    return f"{latitude:.6f}", f"{longitude:.6f}"


def _precompute_map_center(profile: Profile, total: int) -> tuple[float, float] | None:
    """Store the account's map centre without computing it the expensive way.

    `Profile.compute_map_center` finds the densest cluster of an account's pins,
    on the critical path of `view_map`. It used to do that by comparing every
    point with every other one, which at 20,000 pins was around seven minutes of
    a process serving nothing — a load run against a seeded account measured that
    one defect in every phase and nothing else (P108, since fixed: the same
    answer now costs about 99 ms at that size).

    Still stored directly, because a variable held constant is worth holding
    whether or not it is currently large, and because the harness should not
    silently start measuring this again if the algorithm regresses.

    The stored value is the same answer, not an approximation. The seeded grid
    spans well under the 1,000 km cluster radius, so every point is in the one
    cluster and the densest-cluster centroid *is* the arithmetic mean — which
    this computes in one pass.

    Args:
        profile: The seeded account.
        total: How many pins it now has.

    Returns:
        The stored ``(latitude, longitude)``, or None when there was nothing to
        average.
    """
    if total <= 0:
        return None
    points = [_grid(index) for index in range(total)]
    latitude = sum(float(lat) for lat, _ in points) / total
    longitude = sum(float(lng) for _, lng in points) / total
    Profile.objects.filter(pk=profile.pk).update(map_center_latitude=latitude, map_center_longitude=longitude)
    profile.map_center_latitude = latitude
    profile.map_center_longitude = longitude
    return latitude, longitude


def seed_heavy_account(profile: Profile, *, pins: int, analyze: bool = True, precompute_map_center: bool = True) -> dict[str, Any]:
    """Give *profile* *pins* root pins, all carrying one shared label.

    Idempotent in the sense that matters for a fixture: it counts what the
    profile already has and creates only the difference, so re-running against a
    seeded account is fast and does not double it.

    Args:
        profile: The account to seed.
        pins: How many root pins it should end up with.
        analyze: Refresh planner statistics afterwards. Only turn this off to
            demonstrate what skipping it costs.
        precompute_map_center: Store the map centre directly instead of leaving
            the first page load to derive it. On by default because deriving it
            was P108, which is fixed - turn this off to have the centre
            computed the way a real first page load computes it.

    Raises:
        ValueError: ``pins`` is larger than the coordinate scheme can lay out.

    Returns:
        What was done, for the provisioning manifest: the final pin count, how
        many were created now, the shared label's id and name, whether `ANALYZE`
        ran, and how long it took. The manifest carries `analyzed` so a run
        against an unanalysed account is visible in its own output rather than
        inferred later from a strange number, and `label_id` because the load
        harness edits that label by id - looking it up by name would break the
        moment a run renamed it.
    """
    if pins > MAX_SEEDED_PINS:
        raise ValueError(f"{pins} pins would run the grid past the north pole; this coordinate scheme tops out at {MAX_SEEDED_PINS}.")

    started = time.perf_counter()
    existing = Pin.objects.filter(profile=profile).root_pins().count()
    wanted = max(pins - existing, 0)

    label, _ = Label.objects.get_or_create(
        name=HEAVY_LABEL_NAME,
        kind="tag",
        profile=profile,
        defaults={"color": "#b34747", "description": "Every pin in a seeded performance account carries this."},
    )

    created = 0
    for start in range(0, wanted, BATCH_SIZE):
        count = min(BATCH_SIZE, wanted - start)
        with transaction.atomic():
            coordinates = [_grid(existing + start + offset) for offset in range(count)]
            locations = _locations_for(coordinates, first_index=existing + start)
            seeded_pins = Pin.objects.bulk_create(
                [
                    Pin(
                        profile=profile,
                        location=location,
                        name=f"{PIN_NAME_PREFIX} {existing + start + offset}",
                        # Set explicitly because `bulk_create` does not call
                        # `save`, which is where `ensure_slug` runs. A null slug
                        # is legal - the payload falls back to the uuid - but it
                        # would put every seeded pin on a code path real pins do
                        # not take, which is the wrong thing for a fixture whose
                        # whole job is to behave like production at size.
                        slug=f"perf-pin-{existing + start + offset}",
                    )
                    for offset, location in enumerate(locations)
                ],
            )
            through = Pin.labels.through
            through.objects.bulk_create([through(pin_id=pin.pk, label_id=label.pk) for pin in seeded_pins])
        created += len(seeded_pins)

    analyzed = analyze and _analyze()
    centre = _precompute_map_center(profile, existing + created) if precompute_map_center else None
    return {
        "map_center": list(centre) if centre else None,
        "pins": existing + created,
        "created": created,
        "already_present": existing,
        "label": label.name,
        "label_id": label.pk,
        "label_kind": label.kind,
        "pin_name_prefix": PIN_NAME_PREFIX,
        "analyzed": analyzed,
        "seconds": round(time.perf_counter() - started, 1),
    }


def _locations_for(coordinates: list[tuple[str, str]], *, first_index: int) -> list[Location]:
    """The `Location` rows for *coordinates*, creating only the missing ones.

    `Location` is unique on ``(latitude, longitude)`` **globally**, not per
    profile, so the grid is a shared resource: a second seeded account lands on
    the same coordinates as the first and cannot simply create them. Reusing the
    existing row is also the truthful thing to do - it is what the importer does
    when two users pin the same place - and it makes the seeder idempotent
    against a previous run that failed halfway.

    Args:
        coordinates: ``(latitude, longitude)`` string pairs, in pin order.
        first_index: Index of the first coordinate, for naming new rows.

    Returns:
        One `Location` per coordinate, in the same order.

    Raises:
        RuntimeError: A coordinate was neither found nor created, which would
            mean the grid produced a value the database rounded differently -
            silently pairing pins with the wrong places.
    """
    Location.objects.bulk_create(
        [Location(latitude=lat, longitude=lng, official_name=f"Perf Place {first_index + offset}") for offset, (lat, lng) in enumerate(coordinates)],
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


def _analyze() -> bool:
    """Refresh planner statistics for the tables the seed wrote to.

    Named tables rather than a bare ``ANALYZE``: measured on this schema's 237
    tables, whole-database is 3.55s cold and 1.70s warm against 45ms for three
    named ones.

    Returns:
        Whether it ran. False on a non-PostgreSQL backend, which no deployment
        uses but a developer's sqlite experiment might.
    """
    if connection.vendor != "postgresql":
        return False
    with connection.cursor() as cursor:
        cursor.execute("ANALYZE " + ", ".join(f'"{table}"' for table in _ANALYZED_TABLES))
    return True
