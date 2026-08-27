"""Management command to diagnose Google Places API issues, particularly REQUEST_DENIED on CID lookups."""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand
import requests

from urbanlens.dashboard.services.apis.locations.google.geocoding import GoogleGeocodingGateway
from urbanlens.dashboard.services.security.redact import redact_coordinate, redact_secret
from urbanlens.UrbanLens.settings.app import settings as app_settings

# A well-known place with a stable place_id and URL (Empire State Building)
KNOWN_PLACE_ID = "ChIJaXQRs6lZwokRY6EFpJnhNNE"
KNOWN_LAT = 40.7484
KNOWN_LNG = -73.9967
KNOWN_URL = "https://www.google.com/maps/place/Empire+State+Building/@40.7484405,-73.9856644,17z/data=!3m1!4b1!4m6!3m5!1s0x89c259a9b3117469:0xd134e199a405a163!8m2!3d40.7484405!4d-73.9856644"

PLACES_DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"
NEARBY_SEARCH_URL = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
FIND_PLACE_URL = "https://maps.googleapis.com/maps/api/place/findplacefromtext/json"


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

    def _print_result(self, label: str, response: requests.Response) -> dict:
        """Report a single API call's outcome via ``self.stdout``.

        Args:
            label: Human-readable name of the test being reported.
            response: The raw HTTP response from the Google API call.

        Returns:
            The parsed JSON body (or ``{"raw": <text>}`` when it isn't valid JSON).
        """
        try:
            body = response.json()
        except ValueError:
            body = {"raw": response.text}

        status = body.get("status", "N/A")
        error_message = body.get("error_message", "")
        ok = status == "OK"
        style = self.style.SUCCESS if ok else self.style.ERROR
        symbol = "PASS" if ok else "FAIL"
        self.stdout.write(style(f"  [{symbol}] {label}"))
        self.stdout.write(f"         Status: {status}")
        if error_message:
            self.stdout.write(self.style.WARNING(f"         Error:  {error_message}"))
        if not ok:
            self.stdout.write(f"         Body:   {json.dumps(body, indent=10)[:800]}")
        self.stdout.write("")
        return body

    def handle(self, *args, **options):
        key = options["key"] or app_settings.google_unrestricted_api_key
        cid = options["cid"]
        url = options["url"]

        self.stdout.write("")
        self.stdout.write("=" * 60)
        self.stdout.write("  Google Places API Diagnostic")
        self.stdout.write("=" * 60)
        self.stdout.write(f"  google_places_api_key : {redact_secret(app_settings.google_unrestricted_api_key)}")
        self.stdout.write(f"  google_maps_api_key   : {redact_secret(app_settings.google_unrestricted_api_key)}")
        self.stdout.write(f"  Using key             : {redact_secret(key)}")
        self.stdout.write(f"  Test CID              : {cid}")
        self.stdout.write("")

        if not key:
            self.stdout.write(self.style.ERROR("ERROR: No API key configured. Set UL_GOOGLE_UNRESTRICTED_API_KEY in your .env file."))
            return

        session = requests.Session()

        # ------------------------------------------------------------------
        # Test 1: Nearby search - confirms the key works for Places API at all
        # ------------------------------------------------------------------
        self.stdout.write("--- Test 1: Nearby Search (confirms Places API enabled) ---")
        resp = session.get(
            NEARBY_SEARCH_URL,
            params={
                "location": f"{KNOWN_LAT},{KNOWN_LNG}",
                "radius": 100,
                "key": key,
            },
            timeout=10,
        )
        body1 = self._print_result("Nearby Search", resp)

        # ------------------------------------------------------------------
        # Test 2: Find Place from text - another basic endpoint
        # ------------------------------------------------------------------
        self.stdout.write("--- Test 2: Find Place from Text ---")
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
        self._print_result("Find Place from Text", resp)

        # ------------------------------------------------------------------
        # Test 3: Place Details with a known stable place_id
        # ------------------------------------------------------------------
        self.stdout.write("--- Test 3: Place Details with standard place_id ---")
        resp = session.get(
            PLACES_DETAILS_URL,
            params={
                "place_id": KNOWN_PLACE_ID,
                "fields": "name,geometry",
                "key": key,
            },
            timeout=10,
        )
        self._print_result(f"Place Details (place_id={KNOWN_PLACE_ID[:12]}...)", resp)

        # ------------------------------------------------------------------
        # Test 4: CID lookup - formerly-broken format: ?cid=NUMBER
        # This is what the code used to do in geocoding.py's get_coordinates_by_cid,
        # before it was fixed to use the place_id=cid:NUMBER form proven by Test 5.
        # ------------------------------------------------------------------
        self.stdout.write("--- Test 4: CID lookup - ?cid=NUMBER ---")
        resp = session.get(
            PLACES_DETAILS_URL,
            params={
                "cid": str(cid),
                "fields": "geometry",
                "key": key,
            },
            timeout=10,
        )
        self._print_result(f"Place Details cid={cid} [broken format]", resp)

        # ------------------------------------------------------------------
        # Test 5: CID lookup - CORRECT format: ?place_id=cid:NUMBER
        # ------------------------------------------------------------------
        self.stdout.write("--- Test 5: CID lookup - ?place_id=cid:NUMBER) ---")
        resp = session.get(
            PLACES_DETAILS_URL,
            params={
                "place_id": f"cid:{cid}",
                "fields": "geometry",
                "key": key,
            },
            timeout=10,
        )
        self._print_result(f"Place Details place_id=cid:{cid} [correct format]", resp)

        # ------------------------------------------------------------------
        # Test 6: CID lookup with maps_api_key instead (in case wrong key is configured)
        # ------------------------------------------------------------------
        maps_key = app_settings.google_unrestricted_api_key
        if maps_key and maps_key != key:
            self.stdout.write("--- Test 6: CID lookup with google_maps_api_key instead ---")
            self.stdout.write("  (Testing in case your CID lookups should use the Maps key)")
            resp = session.get(
                PLACES_DETAILS_URL,
                params={
                    "place_id": f"cid:{cid}",
                    "fields": "geometry",
                    "key": maps_key,
                },
                timeout=10,
            )
            self._print_result(f"Place Details cid:{cid} [maps key]", resp)

        # ------------------------------------------------------------------
        # Test 7: Exercise the real GoogleGeocodingGateway code paths
        # ------------------------------------------------------------------
        self.stdout.write("--- Test 7: App code - GoogleGeocodingGateway ---")
        self.stdout.write(f"  Note: gateway uses google_maps_api_key ({redact_secret(app_settings.google_unrestricted_api_key)})")
        self.stdout.write("        (not google_places_api_key - CID lookups go through the Maps key)")
        self.stdout.write("")

        gateway_key = app_settings.google_unrestricted_api_key
        gateway = None
        if not gateway_key:
            self.stdout.write(self.style.WARNING("  [SKIP] google_maps_api_key is not set - cannot instantiate GoogleGeocodingGateway\n"))
        else:
            try:
                gateway = GoogleGeocodingGateway(api_key=gateway_key)
            except (TypeError, ValueError) as exc:
                self.stdout.write(self.style.ERROR(f"  [FAIL] Could not instantiate GoogleGeocodingGateway: {exc}\n"))
                gateway = None

            if gateway:
                # 7a: get_coordinates_by_cid
                self.stdout.write(f"  7a: get_coordinates_by_cid({cid})")
                try:
                    lat, lon = gateway.get_coordinates_by_cid(cid)
                    if lat is not None and lon is not None:
                        self.stdout.write(self.style.SUCCESS(f"  [PASS] Resolved to ({redact_coordinate(lat)}, {redact_coordinate(lon)})"))
                    else:
                        self.stdout.write(self.style.ERROR("  [FAIL] Returned (None, None) - CID not in Places database or key rejected"))
                except (OSError, ValueError) as exc:
                    self.stdout.write(self.style.ERROR(f"  [FAIL] Exception: {exc}"))
                self.stdout.write("")

                # 7b: extract_coordinates_from_url - full end-to-end code path
                self.stdout.write("  7b: extract_coordinates_from_url")
                self.stdout.write(f"       URL: {url}")
                try:
                    lat, lon = gateway.extract_coordinates_from_url(url)
                    if lat is not None and lon is not None:
                        self.stdout.write(self.style.SUCCESS(f"  [PASS] Resolved to ({redact_coordinate(lat)}, {redact_coordinate(lon)})"))
                    else:
                        self.stdout.write(self.style.ERROR("  [FAIL] Returned (None, None) - both CID lookup and name geocoding failed"))
                except (OSError, ValueError) as exc:
                    self.stdout.write(self.style.ERROR(f"  [FAIL] Exception: {exc}"))
                self.stdout.write("")

        self.stdout.write("=" * 60)
        self.stdout.write("  Summary")
        self.stdout.write("=" * 60)
        t1_ok = body1.get("status") == "OK"
        if not t1_ok:
            self.stdout.write(self.style.WARNING("  ! Nearby Search failed - the key itself may be invalid or"))
            self.stdout.write(self.style.WARNING("    the Places API is not enabled for this key."))
        else:
            self.stdout.write(self.style.SUCCESS("  + Basic Places API calls (Tests 1-3) succeeded."))
        if gateway_key and gateway_key != key:
            self.stdout.write(self.style.WARNING("  ! google_maps_api_key and google_places_api_key are different keys."))
            self.stdout.write(self.style.WARNING("    CID lookups (Test 7) use the Maps key; check its restrictions separately."))
        self.stdout.write("")
