#!/usr/bin/env python
"""What one account's map data costs to build, to store, and to ship.

D12 rests on two numbers that were inherited rather than measured: the database
projection path costs about 33 ms per 1,000 pins, and the per-pin Valkey cache
holds about 1.7 KB per pin. Both decide whether the cache is worth its shape, so
they need to be reproducible rather than quoted.

Runs against a throwaway test database it creates and destroys, so it is safe on
a machine with real data. Valkey is measured against whatever ``UL_VALKEY_URL``
points at, under this profile's own key prefix, and the keys are deleted
afterwards - so point it at a development instance, not a shared one.

    docker exec -e UL_TEST_DB_NAME=payload_cost <app> \
        /app/.venv/bin/python bin/perf/measure_map_payload.py --pins 10000
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time

sys.path.insert(0, "/app/src" if os.path.isdir("/app/src") else "src")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "urbanlens.UrbanLens.settings")

import django

django.setup()

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import setup_test_environment, teardown_test_environment

#: Rows per batch, matching what the payload service pages at.
BATCH = 1_000


def measure(pins: int, batch: int) -> dict[str, object]:
    """Build one account's map data every way the application can, and time each.

    Args:
        pins: How many pins to seed.
        batch: Page size for the paged measurements.

    Returns:
        A report, keyed by measurement name.
    """
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.integration_testing.perf_seed import seed_heavy_account
    from urbanlens.dashboard.services.map_pins import MapPinCache, MapPinPayloadService

    user = User.objects.create_user(username="payload-cost", email="payload-cost@example.invalid", password="x")  # nosec B106  # noqa: S106
    profile = Profile.objects.get_or_create(user=user)[0]
    seeded = seed_heavy_account(profile, pins=pins, analyze=True)

    query = Pin.objects.filter(profile=profile).root_pins()
    service = MapPinPayloadService(profile)
    report: dict[str, object] = {"pins_requested": pins, "pins_seeded": seeded.get("pins", pins)}

    # The paged path, which is what the map page actually walks today.
    pages, rows, wall, cpu = 0, 0, 0.0, 0.0
    cursor = None
    while True:
        started, started_cpu = time.perf_counter(), time.process_time()
        page = service.page(query, cursor=cursor, limit=batch)
        wall += time.perf_counter() - started
        cpu += time.process_time() - started_cpu
        if not page.pins:
            break
        pages += 1
        rows += len(page.pins)
        cursor = page.next_cursor
        if cursor is None:
            break
    report["paged"] = _per_thousand(rows, wall, cpu, extra={"pages": pages})

    # One document, which is what D12 proposes shipping.
    started, started_cpu = time.perf_counter(), time.process_time()
    everything = service.all(query)
    document_wall = time.perf_counter() - started
    document_cpu = time.process_time() - started_cpu
    ndjson = b"".join(json.dumps(pin, separators=(",", ":")).encode() + b"\n" for pin in everything)
    compressed = gzip.compress(ndjson, compresslevel=6)
    report["document"] = _per_thousand(
        len(everything),
        document_wall,
        document_cpu,
        extra={
            "bytes": len(ndjson),
            "bytes_per_pin": round(len(ndjson) / max(len(everything), 1), 1),
            "gzipped_bytes": len(compressed),
            "gzipped_bytes_per_pin": round(len(compressed) / max(len(everything), 1), 1),
        },
    )

    report["cache"] = _measure_cache(MapPinCache(profile), query, len(everything))
    report["filter_post"] = _measure_filter_post(user, len(everything))
    return report


def _measure_filter_post(user: object, pin_count: int) -> dict[str, object]:
    """What one press of the map's filter button sends over the wire.

    The filter form posts to `map.search`, which renders the whole matching set
    into an HTML document. That is the response a user produces by typing in the
    filter box, so its size is the per-keystroke cost one account can impose on
    the worker serving it.

    Args:
        user: The account to post as.
        pin_count: How many pins matched, for the per-pin figures.

    Returns:
        Status, byte counts and timings, or a note if the endpoint refused.
    """
    from django.test import Client
    from django.urls import reverse

    client = Client()
    client.force_login(user)
    url = reverse("map.search")

    started, started_cpu = time.perf_counter(), time.process_time()
    response = client.post(url, data={}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    wall = time.perf_counter() - started
    cpu = time.process_time() - started_cpu

    body = response.content
    if response.status_code != 200:
        return {"status": response.status_code, "body": body[:300].decode("utf-8", "replace")}
    return _per_thousand(
        pin_count,
        wall,
        cpu,
        extra={
            "status": response.status_code,
            "content_type": response.headers.get("Content-Type", ""),
            "bytes": len(body),
            "bytes_per_pin": round(len(body) / max(pin_count, 1), 1),
            "gzipped_bytes": len(gzip.compress(body, compresslevel=6)),
        },
    )


def _measure_cache(cache: object, query: object, pin_count: int) -> dict[str, object]:
    """Rebuild the per-pin Valkey cache and ask Valkey what it cost.

    ``MEMORY USAGE`` is the server's own accounting, which is the only honest
    answer here: the payload's JSON length ignores the hash's per-field overhead
    and the sorted set entirely.

    Args:
        cache: A ``MapPinCache`` for the profile.
        query: The profile's root pins.
        pin_count: How many pins were serialized, for the per-pin figures.

    Returns:
        Timings and byte counts, or a note saying why they are absent.
    """
    client = getattr(cache, "client", None)
    if client is None:
        return {"skipped": "no UL_VALKEY_URL/UL_REDIS_URL in this environment"}

    started, started_cpu = time.perf_counter(), time.process_time()
    cache.rebuild(query)  # type: ignore[attr-defined]
    wall = time.perf_counter() - started
    cpu = time.process_time() - started_cpu

    keys = {name: getattr(cache, f"{name}_key") for name in ("pins", "order", "meta")}
    try:
        usage = {name: int(client.memory_usage(key) or 0) for name, key in keys.items()}
    finally:
        client.delete(*keys.values())

    total = sum(usage.values())
    return _per_thousand(
        pin_count,
        wall,
        cpu,
        extra={
            "valkey_bytes": total,
            "valkey_bytes_per_pin": round(total / max(pin_count, 1), 1),
            "by_key": usage,
        },
    )


def _per_thousand(rows: int, wall: float, cpu: float, *, extra: dict[str, object] | None = None) -> dict[str, object]:
    """Normalise a timing to the per-1,000-rows figure the design argues in.

    Args:
        rows: How many rows the timing covers.
        wall: Elapsed seconds.
        cpu: Process CPU seconds.
        extra: Measurement-specific fields to merge in.

    Returns:
        The normalised report.
    """
    scale = 1000.0 / rows if rows else 0.0
    report: dict[str, object] = {
        "rows": rows,
        "wall_ms": round(wall * 1000, 1),
        "cpu_ms": round(cpu * 1000, 1),
        "wall_ms_per_1000": round(wall * 1000 * scale, 1),
        "cpu_ms_per_1000": round(cpu * 1000 * scale, 1),
    }
    report.update(extra or {})
    return report


def main() -> int:
    """Parse arguments, run the measurement in a throwaway database, print JSON.

    Returns:
        Process exit status.
    """
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pins", type=int, default=10_000, help="How many pins to seed (default: 10000).")
    parser.add_argument("--batch", type=int, default=BATCH, help=f"Page size for the paged path (default: {BATCH}).")
    args = parser.parse_args()

    setup_test_environment()
    connection.creation.create_test_db(verbosity=0, autoclobber=True)
    try:
        report = measure(args.pins, args.batch)
    finally:
        connection.creation.destroy_test_db(connection.settings_dict["NAME"], verbosity=0)
        teardown_test_environment()

    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
