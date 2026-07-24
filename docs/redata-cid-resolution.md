# REData: Google Maps CID → coordinate resolution

## The problem

UrbanLens imports pins from Google Takeout "Saved Places" exports. Many saved places carry a
Google Maps CID (parsed from the `!1s0x{s2_hex}:0x{cid_hex}` segment of the place's URL) instead
of, or in addition to, literal coordinates. Historically, UrbanLens decoded `s2_hex` directly as an
S2 cell ID to get a free, no-API-call coordinate guess.

That guess is wrong far more often than expected. We measured it directly: 400 real saved places,
each cross-checked against Google's paid Places Details API (which resolves a CID to a ground-truth
coordinate). **31.3% of the sample was wrong by more than 500m.** Some were off by a few km (often
rural/international places where Google's own indexing is sparse); a few were catastrophically
wrong — e.g. "Stillwater Blockhouse" (a real place in upstate NY) decoded to a point ~8,567km away,
on the other side of the globe, because the CID's hex isn't reliably an S2 cell at all — it just
sometimes happens to parse as a structurally-valid one.

Raw data, for reference:
- `notes/geocoding-analysis/cid_ground_truth.json` — 400-place dataset: free S2-decode guess vs.
  paid-API ground truth, with distance error.
- `notes/geocoding-analysis/confirmed_mismatches.json` — the 120 confirmed-wrong places from that
  set, each with both the wrong guess and the correct coordinate.

This can't be fixed with a better local heuristic — we looked (S2 cell level, leaf-ness, title
language, multi-list membership; see the mismatches dataset). The only fix is an actual lookup
against Google's own data for the CID.

## The proof of concept

`src/urbanlens/dashboard/services/apis/locations/google/scraping.py` — `GoogleMapsScraper` — proves
this is solvable without per-call API billing. It drives a real headless browser (Playwright/
Chromium) to `https://www.google.com/maps?cid={cid}`, waits for Google's own client-side JS to
resolve the place, and reads the coordinates back out of the URL it redirects to (the
`!3d{lat}!4d{lon}` pair in the resolved URL's data segment, falling back to the `/@{lat},{lon}`
viewport-center pair).

Validated in `notes/geocoding-analysis/validate_scraper.py` /
`notes/geocoding-analysis/scraper_validation.json`: 60 places sampled (30 known-good, 30
known-bad per the ground-truth dataset above), **60/60 agreed with the paid Places API within
100m**, including every one of the catastrophic S2-decode failures. Zero failed extractions.

This is the reference behavior REData's implementation should replicate (at whatever scale/
infrastructure — proxy pools, browser farms, etc. — makes sense on your end; our POC deliberately
does none of that, see the caveats in `scraping.py`'s module docstring).

## What UrbanLens needs from REData

A CID → coordinate resolution endpoint. UrbanLens will call it from a background job (a pin import
can involve anywhere from a few to several thousand CIDs at once), so a **batch** shape is strongly
preferred over one-CID-per-request.

### Proposed contract (not yet built on REData's side — open to adjustment)

```
POST /api/v1/places/resolve-cids/
Authorization: Bearer <api key>   (same as the existing property-records API)

Request:
{
  "cids": [6952009488037205194, 1234567890123456789, ...]
}

Response: 200
{
  "results": {
    "6952009488037205194": {"lat": 40.4509922, "lng": -78.563521},
    "1234567890123456789": null
  }
}
```

- `results` keys are the requested CIDs (as strings, since JSON object keys can't be numbers).
- A value of `null` means REData confirmed there's no resolvable location for that CID — a
  terminal "no answer," not "try again later." UrbanLens will not retry these.
- Any HTTP-level failure (non-200, timeout, malformed body) is treated as fully transient — the
  whole batch gets retried later with backoff. UrbanLens does **not** fall back to calling Google
  directly when REData is configured, so an endpoint that's flaky or slow to come online will
  visibly delay pin placement for UrbanLens users with REData configured, rather than silently
  degrading — flag it to us if that's a problem so we can revisit that choice.
- We assumed a synchronous response above for simplicity. If resolving a large batch server-side
  needs to be async (submit → poll), that's fine, but our client will need a small follow-up change
  to match — let us know before/while implementing so we can coordinate.
- Follows the same auth/response conventions as the existing property-records API this same
  UrbanLens codebase already talks to
  (`src/urbanlens/dashboard/services/apis/property_records/redata_gateway.py` — bearer token,
  `GET/POST` + JSON, `UL_REDATA_API_URL`/`UL_REDATA_API_KEY`).

### Where this plugs in on our side

`src/urbanlens/dashboard/services/apis/locations/cid_resolution.py` — `resolve_cids()` — is the
single chokepoint that decides REData vs. a direct-Google-Places fallback (used only for
installs that never configured REData at all — see that module for details). Once this endpoint
exists, `src/urbanlens/dashboard/services/apis/locations/google/redata_cid_gateway.py`
(`RedataCidGateway.resolve_cids`) is the client that calls it.
