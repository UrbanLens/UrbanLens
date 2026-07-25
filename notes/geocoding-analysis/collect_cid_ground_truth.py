"""
One-off diagnostic script (not part of the app).

Reads Google Takeout "Saved Places" CSVs, extracts every place URL that carries a
!1s0x{S2_HEX}:0x{CID_HEX} feature-id segment, decodes the S2 hex the same way
GoogleGeocodingGateway._decode_s2_cell does, then calls the real Google Places
Details API (by CID) for a random sample of them to get ground-truth coordinates.

Output is a JSON dataset (one record per sampled place) saved next to this
script, so it can be reused for pattern analysis without spending API calls again.

Usage:
    .venv_windows/Scripts/python.exe notes/geocoding-analysis/collect_cid_ground_truth.py
"""

from __future__ import annotations

import csv
import glob
import json
import math
import random
import re
import time
from pathlib import Path

import requests
import s2sphere

TAKEOUT_DIR = Path(r"C:\Users\jessa\Downloads\Takeout\Saved")
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
OUTPUT_FILE = Path(__file__).resolve().parent / "cid_ground_truth.json"

DATA_SEGMENT_RE = re.compile(r"!1s0x([0-9a-fA-F]+):0x([0-9a-fA-F]+)")
SAMPLE_SIZE = 400
SEED = 42
REQUEST_DELAY_SECONDS = 0.1


def load_api_key() -> str:
    text = ENV_FILE.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("UL_GOOGLE_UNRESTRICTED_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("UL_GOOGLE_UNRESTRICTED_API_KEY not found in .env")


def decode_s2_cell(s2_hex: str) -> tuple[float | None, float | None, int | None]:
    cell = s2sphere.CellId(int(s2_hex, 16))
    if not cell.is_valid():
        return None, None, None
    ll = cell.to_lat_lng()
    lat, lon = ll.lat().degrees, ll.lng().degrees
    if -90 <= lat <= 90 and -180 <= lon <= 180:
        return lat, lon, cell.level()
    return None, None, None


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def collect_unique_places() -> dict[str, dict]:
    unique: dict[str, dict] = {}
    for path in glob.glob(str(TAKEOUT_DIR / "*.csv")):
        list_name = Path(path).stem
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                url = (row.get("URL") or "").strip()
                title = (row.get("Title") or "").strip()
                if not url:
                    continue
                m = DATA_SEGMENT_RE.search(url)
                if not m:
                    continue
                s2_hex, cid_hex = m.group(1).lower(), m.group(2).lower()
                cid_dec = int(cid_hex, 16)
                if cid_hex not in unique:
                    unique[cid_hex] = {
                        "cid_hex": cid_hex,
                        "cid_decimal": cid_dec,
                        "s2_hex": s2_hex,
                        "title": title,
                        "url": url,
                        "lists": [],
                    }
                if list_name not in unique[cid_hex]["lists"]:
                    unique[cid_hex]["lists"].append(list_name)
    return unique


def fetch_ground_truth(cid_decimal: int, api_key: str) -> dict:
    params = {"cid": str(cid_decimal), "fields": "geometry", "key": api_key}
    resp = requests.get(
        "https://maps.googleapis.com/maps/api/place/details/json",
        params=params,
        timeout=30,
    )
    body = resp.json()
    status = body.get("status")
    loc = body.get("result", {}).get("geometry", {}).get("location", {})
    return {
        "api_status": status,
        "api_lat": loc.get("lat"),
        "api_lon": loc.get("lng"),
    }


def main() -> None:
    api_key = load_api_key()
    unique_places = collect_unique_places()
    print(f"Found {len(unique_places)} unique CID places across Takeout CSVs.")

    rng = random.Random(SEED)
    cids = list(unique_places.keys())
    sample_cids = rng.sample(cids, min(SAMPLE_SIZE, len(cids)))
    print(f"Sampling {len(sample_cids)} places for ground-truth lookup.")

    dataset = []
    for i, cid_hex in enumerate(sample_cids, start=1):
        place = unique_places[cid_hex]
        s2_lat, s2_lon, s2_level = decode_s2_cell(place["s2_hex"])

        record = {
            **place,
            "s2_lat": s2_lat,
            "s2_lon": s2_lon,
            "s2_level": s2_level,
        }

        try:
            gt = fetch_ground_truth(place["cid_decimal"], api_key)
        except requests.RequestException as exc:
            gt = {"api_status": f"REQUEST_ERROR: {exc}", "api_lat": None, "api_lon": None}
        record.update(gt)

        if s2_lat is not None and gt.get("api_lat") is not None:
            record["distance_km"] = haversine_km(s2_lat, s2_lon, gt["api_lat"], gt["api_lon"])
        else:
            record["distance_km"] = None

        dataset.append(record)
        if i % 25 == 0 or i == len(sample_cids):
            print(f"  {i}/{len(sample_cids)} done")
        time.sleep(REQUEST_DELAY_SECONDS)

    OUTPUT_FILE.write_text(json.dumps(dataset, indent=2), encoding="utf-8")
    print(f"Saved {len(dataset)} records to {OUTPUT_FILE}")

    resolved = [r for r in dataset if r["distance_km"] is not None]
    mismatches = [r for r in resolved if r["distance_km"] > 0.5]
    print(f"Resolved: {len(resolved)}/{len(dataset)}")
    print(f"Mismatches (>500m): {len(mismatches)} ({len(mismatches) / max(len(resolved), 1):.1%})")


if __name__ == "__main__":
    main()
