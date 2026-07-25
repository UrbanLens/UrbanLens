"""
Validates GoogleMapsScraper against the ground-truth dataset already collected
via the real Places Details API (cid_ground_truth.json). For each sampled place
we already know: (a) what the free S2-decode heuristic says, (b) what the real
paid API says. This script adds (c) what the free browser-scrape says, and checks
whether (c) agrees with (b) - i.e. whether scraping can substitute for the paid
API call.

Usage:
    .venv_windows/Scripts/python.exe notes/geocoding-analysis/validate_scraper.py
"""

from __future__ import annotations

import json
import math
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from urbanlens.dashboard.services.apis.locations.google.scraping import GoogleMapsScraper  # noqa: E402

DATASET_FILE = Path(__file__).resolve().parent / "cid_ground_truth.json"
OUTPUT_FILE = Path(__file__).resolve().parent / "scraper_validation.json"
SAMPLE_SIZE = 60
SEED = 7


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def main() -> None:
    data = json.loads(DATASET_FILE.read_text(encoding="utf-8"))
    resolved = [r for r in data if r["api_lat"] is not None]

    mismatched = [r for r in resolved if r["distance_km"] and r["distance_km"] > 0.5]
    matched = [r for r in resolved if not (r["distance_km"] and r["distance_km"] > 0.5)]

    rng = random.Random(SEED)
    half = SAMPLE_SIZE // 2
    sample = rng.sample(mismatched, min(half, len(mismatched))) + rng.sample(matched, min(half, len(matched)))
    rng.shuffle(sample)
    print(f"Validating scraper against {len(sample)} places ({half} known-mismatch, {half} known-match)")

    results = []
    with GoogleMapsScraper() as scraper:
        for i, record in enumerate(sample, start=1):
            scraped_lat, scraped_lon = scraper.resolve_by_cid(record["cid_decimal"])
            out = {
                "title": record["title"],
                "cid_decimal": record["cid_decimal"],
                "api_lat": record["api_lat"],
                "api_lon": record["api_lon"],
                "scraped_lat": scraped_lat,
                "scraped_lon": scraped_lon,
                "was_known_mismatch": record in mismatched,
            }
            if scraped_lat is not None:
                out["scraper_vs_api_km"] = haversine_km(record["api_lat"], record["api_lon"], scraped_lat, scraped_lon)
            else:
                out["scraper_vs_api_km"] = None
            results.append(out)
            print(f"  {i}/{len(sample)}: {record['title'][:35]:35s} scraper_vs_api={out['scraper_vs_api_km']}")
            time.sleep(0.5)

    OUTPUT_FILE.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")

    agree = [r for r in results if r["scraper_vs_api_km"] is not None and r["scraper_vs_api_km"] <= 0.1]
    failed = [r for r in results if r["scraper_vs_api_km"] is None]
    print()
    print(f"Saved {len(results)} records to {OUTPUT_FILE}")
    print(f"Scraper agrees with paid API within 100m: {len(agree)}/{len(results)} ({len(agree)/len(results):.1%})")
    print(f"Scraper failed to extract coordinates: {len(failed)}/{len(results)}")


if __name__ == "__main__":
    main()
