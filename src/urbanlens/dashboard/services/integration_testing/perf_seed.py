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
from typing import TYPE_CHECKING, Any

from django.db import connection, transaction

from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin

if TYPE_CHECKING:
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

#: Name of the label every seeded pin carries. One shared label is what makes a
#: label edit expensive; see this module's docstring.
HEAVY_LABEL_NAME = "Perf Heavy"

#: Shared by every seeded pin's name, so a load test can post a filter that
#: genuinely matches all of them. A filter matching nothing measures the query
#: planner rather than the response builder.
PIN_NAME_PREFIX = "Perf Pin"

#: Tables whose planner statistics the seed invalidates.
_ANALYZED_TABLES = ("dashboard_locations", "dashboard_user_pins", "dashboard_labels")


def _grid(index: int, side: int) -> tuple[str, str]:
    """One pin's coordinates, laid out on a square grid.

    A grid rather than a line so a bounding-box query over the seeded block
    returns a realistic subset rather than everything or nothing.

    Args:
        index: Which pin, zero-based.
        side: Pins per row of the grid.

    Returns:
        ``(latitude, longitude)`` as strings, since the columns are decimals and
        a float would round differently on the way in.
    """
    latitude = ORIGIN_LATITUDE + (index // side) * COORDINATE_STEP
    longitude = ORIGIN_LONGITUDE + (index % side) * COORDINATE_STEP
    return f"{latitude:.6f}", f"{longitude:.6f}"


def seed_heavy_account(profile: Profile, *, pins: int, analyze: bool = True) -> dict[str, Any]:
    """Give *profile* *pins* root pins, all carrying one shared label.

    Idempotent in the sense that matters for a fixture: it counts what the
    profile already has and creates only the difference, so re-running against a
    seeded account is fast and does not double it.

    Args:
        profile: The account to seed.
        pins: How many root pins it should end up with.
        analyze: Refresh planner statistics afterwards. Only turn this off to
            demonstrate what skipping it costs.

    Returns:
        What was done, for the provisioning manifest: the final pin count, how
        many were created now, the shared label's id and name, whether `ANALYZE`
        ran, and how long it took. The manifest carries `analyzed` so a run
        against an unanalysed account is visible in its own output rather than
        inferred later from a strange number, and `label_id` because the load
        harness edits that label by id - looking it up by name would break the
        moment a run renamed it.
    """
    started = time.perf_counter()
    existing = Pin.objects.filter(profile=profile).root_pins().count()
    wanted = max(pins - existing, 0)

    label, _ = Label.objects.get_or_create(
        name=HEAVY_LABEL_NAME,
        kind="tag",
        profile=profile,
        defaults={"color": "#b34747", "description": "Every pin in a seeded performance account carries this."},
    )

    side = max(int(wanted**0.5) + 1, 1)
    created = 0
    for start in range(0, wanted, BATCH_SIZE):
        count = min(BATCH_SIZE, wanted - start)
        with transaction.atomic():
            locations = Location.objects.bulk_create(
                [
                    Location(
                        latitude=lat,
                        longitude=lng,
                        official_name=f"Perf Place {existing + start + offset}",
                    )
                    for offset in range(count)
                    for lat, lng in [_grid(existing + start + offset, side)]
                ],
            )
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
    return {
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
