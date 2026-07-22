"""Benchmark UrbanLens' Overpass mirror pool against the new self-hosted instance.

Runs an identical set of Overpass QL programs - ranging from a trivial single-node
lookup to region-wide area scans - against every configured endpoint, recording
time-to-first-byte, total wall time, payload size and element count.

Usage:
    python overpass_bench.py [--rounds N] [--out results.json] [--only light|heavy|all]
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from typing import Any

import requests

USER_AGENT = "UrbanLens/1.0 (mirror benchmark; hello@urbanlens.org) python-requests/2.x"

ENDPOINTS: dict[str, str] = {
    "urbanlens (self-hosted)": "https://overpass.osm.urbanlens.org/api/interpreter",
    "overpass-api.de": "https://overpass-api.de/api/interpreter",
    "private.coffee": "https://overpass.private.coffee/api/interpreter",
    "kumi.systems": "https://overpass.kumi.systems/api/interpreter",
    "osm.ch": "https://overpass.osm.ch/api/interpreter",
    "maps.mail.ru": "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
}

# The tag filter UrbanLens actually sends (see boundaries/overpass.py).
TAG_FILTER = '[~"^(building|amenity|tourism|historic|leisure|landuse|industrial|man_made|shop|office)$"~"."]'


def nearby_query(lat: float, lon: float, radius: int, ql_timeout: int = 90) -> str:
    """Reproduce OverpassGateway._nearby_features_query for a coordinate."""
    return f"""
[out:json][timeout:{ql_timeout}];
(
  node(around:{radius},{lat:.7f},{lon:.7f}){TAG_FILTER};
  way(around:{radius},{lat:.7f},{lon:.7f}){TAG_FILTER};
  relation(around:{radius},{lat:.7f},{lon:.7f})["type"="multipolygon"]{TAG_FILTER};
  node(around:{radius},{lat:.7f},{lon:.7f})["railway"="station"];
  way(around:{radius},{lat:.7f},{lon:.7f})["railway"="station"];
  relation(around:{radius},{lat:.7f},{lon:.7f})["type"="multipolygon"]["railway"="station"];
);
out tags geom qt;
""".strip()


# A real-ish parcel ring (Bethlehem Steel site, Bethlehem PA) for the poly: filter,
# matching what plugins.builtin.parcel_buildings sends for a large industrial site.
_STEEL_RING = [
    (40.6120, -75.3800),
    (40.6120, -75.3550),
    (40.6010, -75.3550),
    (40.6010, -75.3800),
]
_POLY_CLAUSE = " ".join(f"{lat:.7f} {lon:.7f}" for lat, lon in _STEEL_RING)


@dataclass(frozen=True)
class Query:
    key: str
    label: str
    weight: str  # light | medium | heavy
    ql: str
    note: str
    rounds: int = 2
    #: Endpoints are hit concurrently by default - they are independent servers,
    #: so queue wait on one does not perturb another. Queries whose payloads are
    #: large enough to saturate the *client's* bandwidth must run serially, or
    #: concurrent transfers would contend and inflate every measurement.
    parallel_safe: bool = True


QUERIES: list[Query] = [
    Query(
        key="ping_node",
        label="Single node by id",
        weight="light",
        note="Pure protocol/round-trip overhead - no real work for the server.",
        ql="[out:json][timeout:60];node(1);out;",
        rounds=3,
    ),
    Query(
        key="pin_urban",
        label="Pin enrichment, dense urban (Manhattan, 250 m)",
        weight="light",
        note="The exact query OverpassGateway.nearby_features sends on pin creation.",
        ql=nearby_query(40.7484, -73.9857, 250),
        rounds=3,
    ),
    Query(
        key="pin_rural",
        label="Pin enrichment, rural (Adirondacks, 250 m)",
        weight="light",
        note="Same query shape over a sparse region - isolates index seek from result size.",
        ql=nearby_query(44.1129, -73.9209, 250),
        rounds=3,
    ),
    Query(
        key="parcel_buildings",
        label="Buildings inside a 3 km parcel polygon (poly: filter)",
        weight="medium",
        note="parcel_buildings plugin query over a large industrial site.",
        ql=f"""
[out:json][timeout:90];
(
  way(poly:"{_POLY_CLAUSE}")["building"];
  relation(poly:"{_POLY_CLAUSE}")["type"="multipolygon"]["building"];
);
out center tags;
""".strip(),
    ),
    Query(
        key="buildings_city_bbox",
        label="All buildings in a 4 km x 3 km city bbox, centroids",
        weight="medium",
        note="Berlin Mitte. Tens of thousands of ways; tests area scan + centroid computation.",
        ql='[out:json][timeout:120];(way["building"](52.500,13.360,52.530,13.420);relation["building"]["type"="multipolygon"](52.500,13.360,52.530,13.420););out center tags;',
    ),
    Query(
        key="buildings_city_geom",
        label="Same bbox, full geometry (out geom)",
        weight="heavy",
        note="Identical selection but every node coordinate is emitted - dominated by payload assembly and transfer.",
        ql='[out:json][timeout:180];(way["building"](52.500,13.360,52.530,13.420););out geom;',
        parallel_safe=False,
        rounds=1,
    ),
    Query(
        key="historic_region",
        label="historic=* across New York State bbox, centroids",
        weight="heavy",
        note="Wide-area scan on a moderately rare tag - the shape of a future 'what is historic near this trip' feature.",
        ql='[out:json][timeout:180];(nwr["historic"](40.50,-79.80,45.02,-71.85););out center tags;',
    ),
    Query(
        key="regex_region",
        label="Regex name match on amenities across a metro bbox",
        weight="heavy",
        note="Regex evaluation over a large candidate set - CPU-bound rather than IO-bound.",
        ql='[out:json][timeout:180];(nwr["amenity"]["name"~"^(Saint|St\\\\.) ",i](40.45,-74.30,41.00,-73.65););out center tags;',
        rounds=1,
    ),
    Query(
        key="ruins_country",
        label="historic=ruins across all of Germany",
        weight="heavy",
        note="Country-scale area scan. The worst realistic case for an under-provisioned instance.",
        ql='[out:json][timeout:180];(nwr["historic"="ruins"](47.20,5.85,55.10,15.05););out center tags;',
        rounds=1,
    ),
    Query(
        key="recurse_admin",
        label="Recurse down a large admin relation (Berlin boundary)",
        weight="heavy",
        note="Relation -> way -> node recursion, then full geometry. Stresses the recursion path rather than area indexing.",
        ql='[out:json][timeout:180];rel(62422);out geom;',
        rounds=2,
    ),
]


@dataclass
class Result:
    query: str
    endpoint: str
    round: int
    ok: bool
    status: int | None = None
    ttfb: float | None = None
    total: float | None = None
    bytes: int | None = None
    elements: int | None = None
    error: str | None = None
    remark: str | None = None


def run_one(session: requests.Session, url: str, ql: str, http_timeout: int) -> tuple[bool, dict[str, Any]]:
    start = time.perf_counter()
    try:
        response = session.post(url, data={"data": ql}, timeout=(10, http_timeout), stream=True)
        ttfb = time.perf_counter() - start
        body = response.content
        total = time.perf_counter() - start
    except requests.RequestException as exc:
        return False, {"error": f"{type(exc).__name__}: {exc}", "total": time.perf_counter() - start}

    info: dict[str, Any] = {"status": response.status_code, "ttfb": ttfb, "total": total, "bytes": len(body)}
    if response.status_code != 200:
        snippet = body[:200].decode("utf-8", "replace").replace("\n", " ")
        info["error"] = f"HTTP {response.status_code}: {snippet}"
        return False, info
    try:
        payload = json.loads(body)
    except ValueError:
        snippet = body[:200].decode("utf-8", "replace").replace("\n", " ")
        info["error"] = f"non-JSON response: {snippet}"
        return False, info
    if "remark" in payload:
        info["remark"] = str(payload["remark"])[:200]
    elements = payload.get("elements")
    info["elements"] = len(elements) if isinstance(elements, list) else 0
    # Overpass reports partial results (timeout/memory abort) via `remark`.
    ok = "remark" not in payload or "runtime error" not in str(payload.get("remark", "")).lower()
    return ok, info


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="overpass_results.json")
    parser.add_argument("--only", default="all", choices=["all", "light", "medium", "heavy", "nonheavy"])
    parser.add_argument("--pause", type=float, default=2.0, help="Seconds between requests to be polite.")
    parser.add_argument("--seed", type=int, default=1337)
    # Matches OverpassGateway.timeout in production: a mirror that needs longer than
    # this has already failed the app, so there is nothing to learn by waiting.
    parser.add_argument("--timeout", type=float, default=30.0, help="Client abort for light/medium queries.")
    parser.add_argument("--heavy-timeout", type=float, default=60.0, help="Client abort for heavy queries.")
    args = parser.parse_args()

    random.seed(args.seed)
    # requests.Session is not thread-safe to share, so each endpoint gets its own.
    sessions: dict[str, requests.Session] = {}
    for name in ENDPOINTS:
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})
        sessions[name] = session

    selected = [
        q
        for q in QUERIES
        if args.only == "all"
        or (args.only == "nonheavy" and q.weight != "heavy")
        or q.weight == args.only
    ]

    results: list[Result] = []
    print_lock = threading.Lock()

    def measure(query: Query, name: str, url: str, round_index: int, http_timeout: int) -> Result:
        ok, info = run_one(sessions[name], url, query.ql, http_timeout)
        with print_lock:
            took = info.get("total") or 0.0
            detail = (
                f"{info.get('elements')} el {(info.get('bytes') or 0) / 1024:.0f} KiB"
                if ok
                else (info.get("error") or "")[:110]
            )
            print(f"[{query.key} r{round_index}] {name:<24} {'OK  ' if ok else 'FAIL'} {took:7.2f}s  {detail}", file=sys.stderr, flush=True)
        return Result(query=query.key, endpoint=name, round=round_index, ok=ok, **info)

    for query in selected:
        http_timeout = args.heavy_timeout if query.weight == "heavy" else args.timeout
        for round_index in range(1, query.rounds + 1):
            order = list(ENDPOINTS.items())
            random.shuffle(order)  # rotate who goes first so warm-cache order doesn't bias a mirror
            if query.parallel_safe:
                with ThreadPoolExecutor(max_workers=len(order)) as pool:
                    futures = [pool.submit(measure, query, name, url, round_index, http_timeout) for name, url in order]
                    results.extend(future.result() for future in futures)
            else:
                for name, url in order:
                    results.append(measure(query, name, url, round_index, http_timeout))
                    time.sleep(args.pause)
            time.sleep(args.pause)

        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump([asdict(r) for r in results], handle, indent=1)

    print(f"\nwrote {len(results)} measurements to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
