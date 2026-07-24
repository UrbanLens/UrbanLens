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

No local heuristic fixes this reliably (S2 cell level, leaf-ness, title language, and multi-list
membership were all checked and none discriminate well enough - see the mismatches dataset). An
actual lookup against Google's own data for the CID is the only fix.

## Resolution: REData

REData resolves a Google Maps CID to a coordinate as a first-class capability of its API. UrbanLens
only depends on that API's contract (below), not on how REData produces an answer internally -
that's REData's own implementation detail, subject to change without notice on this end.

### The live endpoint

`POST /places/resolve-cids/` on REData — see `../REData/docs/api-reference.md`, "Google Maps CID
resolution", for the authoritative contract (REData's code wins if this doc and that one ever
disagree). Summary:

- **Auth**: same bearer-token REData account already used for property records
  (`UL_REDATA_API_URL`/`UL_REDATA_API_KEY`) — the key needs REData's `places:read` scope.
- **Request**: `{"cids": [<int>, ...]}`, up to 10,000 per call (REData returns `400` above that;
  UrbanLens's gateway chunks transparently so callers never have to think about this).
- **Response** (`200`):
  ```jsonc
  {
    "results": {
      "123456789012345678": { "lat": 38.456, "lng": -77.123 },
      "987654321098765432": null   // confirmed, after repeated attempts, unresolvable
    },
    "pending": ["555555555555555555"]   // just queued / in flight server-side - poll again later
  }
  ```
  Resolution is asynchronous on REData's end, so a cid not yet settled comes back in `pending`,
  not as an error or a missing key.
- **Rate limits**: a dedicated 200 requests/hour per API key on this endpoint specifically (on top
  of REData's general 2,000/hour per-key budget), rated in calls, not CIDs — see api-reference.md's
  "Rate limiting" section.

### Where this plugs in on our side

`src/urbanlens/dashboard/services/apis/locations/cid_resolution.py` — `resolve_cids()` — is the
single chokepoint deciding REData vs. a direct-Google-Places fallback (used only for installs that
never configured REData at all - e.g. someone else self-hosting UrbanLens). It returns a
`CidResolutionResult` with `resolved`/`unresolvable`/`pending` buckets regardless of which provider
answered, so callers (`tasks.resolve_deferred_pin_locations`) don't need to know which one ran.

`src/urbanlens/dashboard/services/apis/locations/google/redata_cid_gateway.py`
(`RedataCidGateway.resolve_cids`) is the actual REST client for the endpoint above.
