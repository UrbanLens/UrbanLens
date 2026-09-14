#!/usr/bin/env python
"""What one account's map data costs to build and to ship."""

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


def measure(pins: int, batch: int, labels_per_pin: int) -> dict[str, object]:
    """Build one account's map data every way the application can, and time each.

    Args:
        pins: How many pins to seed.
        batch: Page size for the paged measurements.
        labels_per_pin: How many labels each seeded pin carries.

    Returns:
        A report, keyed by measurement name."""
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.integration_testing.perf_seed import seed_heavy_account
    from urbanlens.dashboard.services.map_pins import MapPinPayloadService

    user = User.objects.create_user(username="payload-cost", email="payload-cost@example.invalid", password="x")  # nosec B106  # noqa: S106
    profile = Profile.objects.get_or_create(user=user)[0]
    seeded = seed_heavy_account(profile, pins=pins, analyze=True, labels_per_pin=labels_per_pin)

    query = Pin.objects.filter(profile=profile).root_pins()
    service = MapPinPayloadService(profile)
    report: dict[str, object] = {"pins_requested": pins, "pins_seeded": seeded.get("pins", pins), "labels_per_pin": labels_per_pin}

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

    report["labels"] = _measure_label_dictionary(service, len(everything))
    report["denormalised"] = _measure_denormalised(service, everything, len(everything))
    report["filter_post"] = _measure_filter_post(user, len(everything))
    return report


def _measure_label_dictionary(service: object, pin_count: int) -> dict[str, object]:
    """What the per-response label dictionary costs, once.

    Args:
        service: The payload service for the profile.
        pin_count: How many pins the response would carry.

    Returns:
        Its size, and what it works out to per pin."""
    started, started_cpu = time.perf_counter(), time.process_time()
    dictionary = service.label_dictionary()  # type: ignore[attr-defined]
    wall = time.perf_counter() - started
    cpu = time.process_time() - started_cpu
    encoded = json.dumps(dictionary, separators=(",", ":")).encode()
    return {
        "entries": len(dictionary),
        "wall_ms": round(wall * 1000, 1),
        "cpu_ms": round(cpu * 1000, 1),
        "bytes": len(encoded),
        "bytes_per_pin": round(len(encoded) / max(pin_count, 1), 2),
    }


def _measure_denormalised(service: object, payloads: list, pin_count: int) -> dict[str, object]:
    """What the same document would weigh with each label copied into every pin.

    The shape the payload had before the labels were normalised out of it, built from the same rows in the same
    run so the two are comparable.

    Args:
        service: The payload service for the profile.
        payloads: The v11 payloads, which name their labels by id.
        pin_count: How many pins those cover.

    Returns:
        Byte counts for the denormalised form."""
    dictionary = service.label_dictionary()  # type: ignore[attr-defined]
    inflated = []
    for payload in payloads:
        copy = {key: value for key, value in payload.items() if key != "label_ids"}
        labels = [dictionary[str(label_id)] for label_id in payload["label_ids"] if str(label_id) in dictionary]
        copy["tags"] = [{"id": label["id"], "name": label["name"], "color": label["color"], "icon": label["icon"]} for label in labels]
        statuses = [label["name"] for label in labels if label["kind"] == "status"]
        copy["status"] = statuses[0] if statuses else ""
        copy["categories"] = [label["name"] for label in labels if label["kind"] == "category"]
        inflated.append(copy)
    ndjson = b"".join(json.dumps(pin, separators=(",", ":")).encode() + b"\n" for pin in inflated)
    compressed = gzip.compress(ndjson, compresslevel=6)
    return {
        "bytes": len(ndjson),
        "bytes_per_pin": round(len(ndjson) / max(pin_count, 1), 1),
        "gzipped_bytes": len(compressed),
        "gzipped_bytes_per_pin": round(len(compressed) / max(pin_count, 1), 1),
    }


def _measure_filter_post(user: object, pin_count: int) -> dict[str, object]:
    """What one press of the map's filter button sends over the wire.

    The filter form posts to `map.search`, which renders the whole matching set into an HTML document.

    Args:
        user: The account to post as.
        pin_count: How many pins matched, for the per-pin figures.

    Returns:
        Status, byte counts and timings, or a note if the endpoint refused."""
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


def _per_thousand(rows: int, wall: float, cpu: float, *, extra: dict[str, object] | None = None) -> dict[str, object]:
    """Normalise a timing to the per-1,000-rows figure the design argues in.

    Args:
        rows: How many rows the timing covers.
        wall: Elapsed seconds.
        cpu: Process CPU seconds.
        extra: Measurement-specific fields to merge in.

    Returns:
        The normalised report."""
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
    parser.add_argument("--labels-per-pin", type=int, default=1, help="Labels each seeded pin carries (default: 1).")
    args = parser.parse_args()

    setup_test_environment()
    connection.creation.create_test_db(verbosity=0, autoclobber=True)
    try:
        report = measure(args.pins, args.batch, args.labels_per_pin)
    finally:
        connection.creation.destroy_test_db(connection.settings_dict["NAME"], verbosity=0)
        teardown_test_environment()

    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
