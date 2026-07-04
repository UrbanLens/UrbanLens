"""Management command to diagnose Google Places API issues, particularly REQUEST_DENIED on CID lookups."""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand
import requests

from urbanlens.dashboard.services.apis.locations.google.geocoding import GoogleGeocodingGateway
from urbanlens.UrbanLens.settings.app import settings as app_settings

# A well-known place with a stable place_id and URL (Empire State Building)
KNOWN_PLACE_ID = "ChIJaXQRs6lZwokRY6EFpJnhNNE"
KNOWN_LAT = 40.7484
KNOWN_LNG = -73.9967
KNOWN_URL = "https://www.google.com/maps/place/Empire+State+Building/@40.7484405,-73.9856644,17z/data=!3m1!4b1!4m6!3m5!1s0x89c259a9b3117469:0xd134e199a405a163!8m2!3d40.7484405!4d-73.9856644"

PLACES_DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"
NEARBY_SEARCH_URL = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
FIND_PLACE_URL = "https://maps.googleapis.com/maps/api/place/findplacefromtext/json"


def _mask(key: str | None) -> str:
    if not key:
        return "(not set)"
    if len(key) <= 8:
        return "***"
    return key[:3] + "..." + key[-3:]


def _print_result(label: str, response: requests.Response) -> None:
    try:
        body = response.json()
    except ValueError:
        body = {"raw": response.text}

    status = body.get("status", "N/A")
    error_message = body.get("error_message", "")
    ok = status == "OK"
    symbol = "PASS" if ok else "FAIL"
    print(f"  [{symbol}] {label}")
    print(f"         Status: {status}")
    if error_message:
        print(f"         Error:  {error_message}")
    if not ok:
        print(f"         Body:   {json.dumps(body, indent=10)[:800]}")
    print()
    return body


class Command(BaseCommand):
    """Runs a series of Places API requests to identify auth and configuration issues."""

    help = "Diagnose Google Places API issues (REQUEST_DENIED, CID lookups, key restrictions)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--cid",
            type=int,
            default=7549064735315116542,
            help="Decimal CID to test (default: the one from your logs)",
        )
        parser.add_argument(
            "--key",
            type=str,
            default=None,
            help="Override the API key (default: UL_GOOGLE_UNRESTRICTED_API_KEY from settings)",
        )
        parser.add_argument(
            "--url",
            type=str,
            default=KNOWN_URL,
            help="Google Maps URL to test end-to-end via extract_coordinates_from_url (default: Empire State Building)",
        )

    def handle(self, *args, **options):
        key = options["key"] or app_settings.google_unrestricted_api_key
        cid = options["cid"]
        url = options["url"]

        print()
        print("=" * 60)
        print("  Google Places API Diagnostic")
        print("=" * 60)
        print(f"  google_places_api_key : {_mask(app_settings.google_unrestricted_api_key)}")
        print(f"  google_maps_api_key   : {_mask(app_settings.google_unrestricted_api_key)}")
        print(f"  Using key             : {_mask(key)}")
        print(f"  Test CID              : {cid}")
        print()

        if not key:
            print("ERROR: No API key configured. Set UL_GOOGLE_UNRESTRICTED_API_KEY in your .env file.")
            return

        session = requests.Session()

        # ------------------------------------------------------------------
        # Test 1: Nearby search - confirms the key works for Places API at all
        # ------------------------------------------------------------------
        print("--- Test 1: Nearby Search (confirms Places API enabled) ---")
        resp = session.get(
            NEARBY_SEARCH_URL,
            params={
                "location": f"{KNOWN_LAT},{KNOWN_LNG}",
                "radius": 100,
                "key": key,
            },
            timeout=10,
        )
        body1 = _print_result("Nearby Search", resp)

        # ------------------------------------------------------------------
        # Test 2: Find Place from text - another basic endpoint
        # ------------------------------------------------------------------
        print("--- Test 2: Find Place from Text ---")
        resp = session.get(
            FIND_PLACE_URL,
            params={
                "input": "Empire State Building",
                "inputtype": "textquery",
                "fields": "name,place_id",
                "key": key,
            },
            timeout=10,
        )
        _print_result("Find Place from Text", resp)

        # ------------------------------------------------------------------
        # Test 3: Place Details with a known stable place_id
        # ------------------------------------------------------------------
        print("--- Test 3: Place Details with standard place_id ---")
        resp = session.get(
            PLACES_DETAILS_URL,
            params={
                "place_id": KNOWN_PLACE_ID,
                "fields": "name,geometry",
                "key": key,
            },
            timeout=10,
        )
        _print_result(f"Place Details (place_id={KNOWN_PLACE_ID[:12]}...)", resp)

        # ------------------------------------------------------------------
        # Test 4: CID lookup - CURRENT (broken) format: ?cid=NUMBER
        # This is what the code currently does at geocoding.py:248
        # ------------------------------------------------------------------
        print("--- Test 4: CID lookup - ?cid=NUMBER ---")
        resp = session.get(
            PLACES_DETAILS_URL,
            params={
                "cid": str(cid),
                "fields": "geometry",
                "key": key,
            },
            timeout=10,
        )
        _print_result(f"Place Details cid={cid} [broken format]", resp)

        # ------------------------------------------------------------------
        # Test 5: CID lookup - CORRECT format: ?place_id=cid:NUMBER
        # ------------------------------------------------------------------
        print("--- Test 5: CID lookup - ?place_id=cid:NUMBER) ---")
        resp = session.get(
            PLACES_DETAILS_URL,
            params={
                "place_id": f"cid:{cid}",
                "fields": "geometry",
                "key": key,
            },
            timeout=10,
        )
        _print_result(f"Place Details place_id=cid:{cid} [correct format]", resp)

        # ------------------------------------------------------------------
        # Test 6: CID lookup with maps_api_key instead (in case wrong key is configured)
        # ------------------------------------------------------------------
        maps_key = app_settings.google_unrestricted_api_key
        if maps_key and maps_key != key:
            print("--- Test 6: CID lookup with google_maps_api_key instead ---")
            print("  (Testing in case your CID lookups should use the Maps key)")
            resp = session.get(
                PLACES_DETAILS_URL,
                params={
                    "place_id": f"cid:{cid}",
                    "fields": "geometry",
                    "key": maps_key,
                },
                timeout=10,
            )
            _print_result(f"Place Details cid:{cid} [maps key]", resp)

        # ------------------------------------------------------------------
        # Test 7: Exercise the real GoogleGeocodingGateway code paths
        # ------------------------------------------------------------------
        print("--- Test 7: App code - GoogleGeocodingGateway ---")
        print(f"  Note: gateway uses google_maps_api_key ({_mask(app_settings.google_unrestricted_api_key)})")
        print("        (not google_places_api_key - CID lookups go through the Maps key)")
        print()

        gateway_key = app_settings.google_unrestricted_api_key
        if not gateway_key:
            print("  [SKIP] google_maps_api_key is not set - cannot instantiate GoogleGeocodingGateway\n")
        else:
            try:
                gateway = GoogleGeocodingGateway(api_key=gateway_key)
            except (TypeError, ValueError) as exc:
                print(f"  [FAIL] Could not instantiate GoogleGeocodingGateway: {exc}\n")
                gateway = None

            if gateway:
                # 7a: get_coordinates_by_cid
                print(f"  7a: get_coordinates_by_cid({cid})")
                try:
                    lat, lon = gateway.get_coordinates_by_cid(cid)
                    if lat is not None and lon is not None:
                        print(f"  [PASS] Resolved to ({lat}, {lon})")
                    else:
                        print("  [FAIL] Returned (None, None) - CID not in Places database or key rejected")
                except (OSError, ValueError) as exc:
                    print(f"  [FAIL] Exception: {exc}")
                print()

                # 7b: extract_coordinates_from_url - full end-to-end code path
                print("  7b: extract_coordinates_from_url")
                print(f"       URL: {url}")
                try:
                    lat, lon = gateway.extract_coordinates_from_url(url)
                    if lat is not None and lon is not None:
                        print(f"  [PASS] Resolved to ({lat}, {lon})")
                    else:
                        print("  [FAIL] Returned (None, None) - both CID lookup and name geocoding failed")
                except (OSError, ValueError) as exc:
                    print(f"  [FAIL] Exception: {exc}")
                print()

        print("=" * 60)
        print("  Summary")
        print("=" * 60)
        t1_ok = body1.get("status") == "OK"
        if not t1_ok:
            print("  ! Nearby Search failed - the key itself may be invalid or")
            print("    the Places API is not enabled for this key.")
        else:
            print("  + Basic Places API calls (Tests 1-3) succeeded.")
        if gateway_key and gateway_key != key:
            print("  ! google_maps_api_key and google_places_api_key are different keys.")
            print("    CID lookups (Test 7) use the Maps key; check its restrictions separately.")
        print()
