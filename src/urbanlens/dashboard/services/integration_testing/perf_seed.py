"""Seed one account large enough for the neighbour test to mean something.
The performance work this exists for is about what *one* user's account costs everyone else, so its measurements need an account big enough for per-row costs to dominate the constants."""

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

#: Largest seed this coordinate scheme can lay out without leaving valid
#: latitudes. `GRID_SIDE` columns per row, `COORDINATE_STEP` degrees per row,
#: starting at `ORIGIN_LATITUDE` and running north.
MAX_SEEDED_PINS = int((90.0 - ORIGIN_LATITUDE) / COORDINATE_STEP) * GRID_SIDE

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


def _grid(index: int) -> tuple[str, str]:
    """One pin's coordinates, laid out on a fixed grid.
    A grid rather than a line so a bounding-box query over the seeded block returns a realistic subset rather than everything or nothing.

    Args:
        index: Which pin, zero-based.

    Returns:
        ``(latitude, longitude)`` as strings, since the columns are decimals and a float would round differently on the way in."""
    latitude = ORIGIN_LATITUDE + (index // GRID_SIDE) * COORDINATE_STEP
    longitude = ORIGIN_LONGITUDE + (index % GRID_SIDE) * COORDINATE_STEP
    return f"{latitude:.6f}", f"{longitude:.6f}"


def _precompute_map_center(profile: Profile, total: int) -> tuple[float, float] | None:
    """Store the account's map centre without computing it the expensive way.

    Args:
        profile: The seeded account.
        total: How many pins it now has.

    Returns:
        The stored ``(latitude, longitude)``, or None when there was nothing to average."""
    if total <= 0:
        return None
    points = [_grid(index) for index in range(total)]
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
) -> dict[str, Any]:
    """Give *profile* *pins* root pins, all carrying one shared label.

    Args:
        profile: The account to seed.
        pins: How many root pins it should end up with.
        analyze: Refresh planner statistics afterwards.
        labels_per_pin: How many labels each pin carries.
        precompute_map_center: Store the map centre directly instead of leaving the first page load to derive it.

    Raises:
        ValueError: ``pins`` is larger than the coordinate scheme can lay out.

    Returns:
        What was done, for the provisioning manifest: the final pin count, how many were created now, the shared label's id and name, whether `ANALYZE` ran, and how long it took."""
    if pins > MAX_SEEDED_PINS:
        raise ValueError(f"{pins} pins would run the grid past the north pole; this coordinate scheme tops out at {MAX_SEEDED_PINS}.")
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
            coordinates = [_grid(existing + start + offset) for offset in range(count)]
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
        "labels_per_pin": labels_per_pin,
        "pin_name_prefix": PIN_NAME_PREFIX,
        "analyzed": analyzed,
        "seconds": round(time.perf_counter() - started, 1),
    }


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

    Returns:
        Whether it ran."""
    if connection.vendor != "postgresql":
        return False
    with connection.cursor() as cursor:
        cursor.execute("ANALYZE " + ", ".join(f'"{table}"' for table in _ANALYZED_TABLES))
    return True
