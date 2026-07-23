# UrbanLens Companion App — Language & Technology Stack (r2)

Revises `mobile-app-stack.md` (decided 2026-07-23). This revision reconciles
the plan with two things that now exist: the Flutter client in the
`android/` repo (feature status: `android/docs/PARITY.md`; assumed server
contract: `android/docs/BACKEND_CHANGES.md`), and the server's v0.6.0
external-API work (delta sync, tombstones, idempotent create, OAuth2+PKCE,
push-device registration, OpenAPI schema, media gate). Decisions unchanged
from r1 are restated briefly; changes are marked **[r2]**.

## Context

Unchanged: map-centric (~8k personal pins, clustered), offline field capture
as a requirement, byte-exact E2EE interop with the web client, AGPL-3.0,
solo developer, Android first with iOS/desktop from the same codebase.

**[r2] Status:** the client is no longer gated on a green-field spike. The
app builds for all five platforms, runs fully against a seeded in-memory
demo world, and has a hand-written API layer. The server, as of v0.6.0,
implements the r1 sync/auth/push prerequisites for the pins domain. The
de-risking items shrink to the two that remain genuinely unproven (map
scale, E2EE byte-exactness).

## Decision: Flutter + Dart — unchanged, now validated

One codebase for Android, iOS, Windows/macOS/Linux. The alternatives table
from r1 stands. **[r2]** Desktop and test targets have been exercised
continuously in CI (analyze + widget tests + Linux/APK debug builds);
nothing has surfaced that challenges the choice.

## Map architecture — **[r2] abstraction narrowed, bake-off still open**

r1 called for a day-one `MapView` abstraction with MapLibre GL on mobile and
`flutter_map` on desktop. What exists is `flutter_map` on every platform,
across five surfaces: the main map, the markup editor, the boundary editor,
filter-region drawing, and pin-detail mini-maps.

**[r2] Revision:** only the main map screen has the ~8k-pin scale problem.
The secondary surfaces render tens of geometries and are fine on
`flutter_map` everywhere, permanently. Therefore:

- The `MapView` abstraction, if MapLibre is adopted, wraps **only the main
  map screen** — a far smaller refactor than r1 assumed.
- Interim (landing now): Dart-side marker clustering on the main map via a
  permissively-licensed supercluster port. This may prove sufficient; the
  bake-off decides.
- The bake-off from r1 remains the gate for MapLibre: render the real
  exported pin dataset (the app's own GeoJSON export can produce it) in
  clustered `flutter_map` vs MapLibre on a mid-range Android device and
  compare frame times. If clustered `flutter_map` holds 60 fps at 8k pins,
  MapLibre (and the abstraction) is dropped; if not, it is adopted for the
  main map only.
- MapLibre's built-in offline regions were half the r1 motivation. The
  license-safe alternative shipped instead: see tile caching below.

## Pin delivery — offline-first local sync — **[r2] server side shipped**

Personal pins sync in full to a local drift (SQLite) DB. The app implements
the cache plus a write-ahead outbox today (reads fall back to cache; offline
mutations queue and replay in order on reconnect).

**[r2] The r1 API design exists in v0.6.0** (`dashboard/external_api/`,
`services/pin_sync.py`):

- `GET pins/` — cursor-paged sync feed ordered `(updated, pk)`:
  `modified_since` + opaque `cursor` → `{pins, next_cursor, sync_watermark}`;
  payload is the map-pin shape plus `pin_type`, `parent_uuid`, `created`,
  `updated`.
- `GET pins/deleted/` — `PinTombstone` feed (`deleted_since` →
  `{tombstones: [{pin_uuid, deleted_at}], next_cursor, sync_watermark}`).
- `POST pins/` — accepts a client-generated `uuid` as the outbox idempotency
  key; replays return the existing pin with `created: false`.

**[r2] Client adaptation required:** the app's hand-written client predates
this and assumed DRF page-number pagination and a full-pin create response —
it is being rewritten against the real feed (cursor walk, watermark
persistence, tombstone eviction, `uuid` on create). Remaining server gap:
create accepts only name/coordinates/address/icon/color — the app's capture
flow also wants `description` and `pin_type` (tracked in BACKEND_CHANGES §2).

Server-side MVT for community locations stays deferred (PostGIS-geometry
bbox prerequisite unchanged).

## Offline pin creation — outbox — unchanged in design, partially built

**[r2]** Implemented app-side: drift outbox for pin create/update/delete
with ordered replay on reconnect; client `uuid` generation landing with the
sync-client rewrite. Still open, in priority order: `workmanager` background
retry with backoff (today replay happens in-app), resumable photo uploads
ordered after their pin's create, and in-app camera capture (today photos
come from the system picker via `file_picker`; the `camera` package remains
the v1-mobile plan).

## Push — **[r2] server half shipped; client not started**

UnifiedPush/ntfy default, FCM as an optional `play` flavor — unchanged.
**[r2]** v0.6.0 ships device registration: `POST push-devices/`
(`transport` defaulting to UnifiedPush, `address`, `name`; idempotent on
address) and `DELETE push-devices/{uuid}/`, behind a dedicated
`push:manage` scope. Client-side UnifiedPush integration and the dispatcher
wiring remain. In-app liveness is covered today by `ws/notifications/` plus
a 60-second unread-poll fallback when WS auth is unavailable.

## E2EE interop — **[r2] library substitution, fixture test outstanding**

r1 specified `sodium_libs` (native libsodium). The implementation instead
uses `pinenacl` (pure-Dart TweetNaCl: X25519, XSalsa20-Poly1305 secretbox
as `nonce ‖ box`, `crypto_box_seal`, standard padded base64) plus the
`cryptography` package for Argon2id. Wire formats match the server scheme,
and KDF parameters are read from server-provided values, never hardcoded —
both r1 rules honored.

**[r2] Two caveats, and the plan:**

- Pure-Dart Argon2id at 64 MiB / opslimit 2 will take seconds on mid-range
  phones vs. sub-second native libsodium. The unlock UX will feel it.
  Target remains `sodium_libs`; the swap is contained behind the app's
  single `E2eeService` class.
- r1's "byte-exact or bust" web-client fixture test has **never run** — the
  app's tests only round-trip within its own Dart implementation. A fixture
  generated by the web client (salts, wrapped private key, a sealed
  conversation key, one encrypted message) belongs in the app's test suite
  before api-mode E2EE ships. This is the highest-value remaining spike
  item.

## Auth — **[r2] decided: OAuth2 + PKCE — and now implemented server-side**

Resolved in favor of OAuth2 + PKCE, per r1 — and
v0.6.0 already mounts django-oauth-toolkit at `/oauth/` with
`PKCE_REQUIRED`, scopes `profile:read` / `pins:read` / `pins:write` /
`push:manage`, 1-hour access tokens, and 90-day rotating refresh tokens.
The external API accepts both credential kinds (PAT `ApiKey` bearer and
OAuth2 access tokens) against the same per-method scope declarations.

Remaining work: client-side `flutter_appauth` integration with per-platform
redirect wiring (Android App Links, iOS Universal Links, desktop loopback);
first-party client registration/provisioning; scope growth as more domains
(lists, wiki, trips, photos, …) get external endpoints. The app's interim
password-exchange proposal (old BACKEND_CHANGES §1) is dropped; PAT keys
remain the prototyping path and continue to work.

**[r2] Mount note:** the app targets `/dashboard/api/external/v1/`, the
current (and for now permanent) mount. The server/agent split described in
split-architecture.md — which would have moved this to `/api/v1/` — is on
hold; if it is ever revisited, the app's base path is one config constant.

## Licensing — unchanged, verified

Client is AGPL-3.0 (LICENSE in repo). **[r2]** Verified: no GPL/AGPL
third-party dependencies; `flutter_map_tile_caching` remains excluded. Tile
caching is the in-house dio+drift layer (below), which also serves mobile
until/unless MapLibre offline regions arrive.

## Tile caching — **[r2] new section**

A small in-house layer (per r1's desktop plan, promoted to all platforms):
a `flutter_map` tile provider backed by the existing drift DB — serve from
cache, fetch and store on miss, LRU-capped. Gives offline field use real
basemaps under the already-offline pins without any GPL dependency.

## Client stack — **[r2] as built**

| Concern | r1 plan | Now |
|---|---|---|
| Map | `maplibre` (mobile) / `flutter_map` (desktop) | `flutter_map` everywhere + clustering; MapLibre pending bake-off, main map only |
| State | Riverpod | Riverpod 2 (classic providers, no codegen) ✓ |
| Navigation | — | go_router, site-mirroring deep links |
| HTTP | dio + OpenAPI-generated client | dio + hand-written contract-first client; server schema now live at `…/v1/schema/` → CI contract validation |
| Models | freezed + json_serializable | ✓ (snake_case wire format) |
| Local DB / outbox | drift | ✓ (pin cache + mutation outbox) |
| Background sync | workmanager | not yet; in-app replay only |
| Auth | flutter_appauth + flutter_secure_storage | secure storage ✓; server OAuth2 live; appauth integration pending; PAT interim |
| WebSockets | web_socket_channel | ✓ (+ notification polling fallback) |
| E2EE | sodium_libs | pinenacl + cryptography (interop formats ✓); sodium_libs still target |
| Camera/GPS | camera, geolocator | geolocator ✓ (my-location); camera pending; file_picker for photo upload |
| Push | UnifiedPush/ntfy; FCM flavor | server registration live; client not started |

## Backend prerequisites — **[r2] status against the r1 list**

`android/docs/BACKEND_CHANGES.md` is the maintained, detailed contract for
everything beyond the pins domain. Against r1's seven prerequisites:

1. Delta-sync endpoints with tombstones — **✅ shipped (v0.6.0)** for pins;
   other domains (lists, labels, trips, …) still to come.
2. Client pin UUIDs on create — **✅ shipped (v0.6.0)**.
3. OAuth2+PKCE — **✅ shipped (v0.6.0)**; client integration pending.
4. Authenticated media — **partial**: `MediaGateView` closes the open-nginx
   hole, but it is session-authenticated only; API-token (PAT/OAuth2) access
   to `/media/` is still required before the app can fetch photos
   (BACKEND_CHANGES §7).
5. Push device registration — **✅ shipped (v0.6.0)**; ntfy dispatcher +
   client integration remain.
6. drf-spectacular schema — **✅ shipped** (`…/v1/schema/`, unauthenticated);
   repurposed to validate the hand-written client contract in CI.
7. MVT tiles for community locations — deferred, unchanged.

Biggest remaining server surface: everything in BACKEND_CHANGES beyond
pins — pin detail/PATCH/DELETE and sub-resources, lists/saved filters,
labels, wikis, trips, messaging + E2EE endpoints with token auth, photo
upload, safety, social, notifications, search — plus API-token WebSocket
auth (§6).

## Remaining de-risking — **[r2] narrowed from the r1 spike**

1. **Map bake-off at real scale** (clustered `flutter_map` vs MapLibre,
   ~8k-pin export, mid-range Android). Decides the MapLibre question.
2. **E2EE byte-exact fixture** from the web client, run in the app's suite.

The r1 spike's other two items are proven: the desktop target builds and
runs the full app; PAT auth round-trips including airplane-mode capture →
reconnect → outbox replay (covered by the app's outbox tests).

## Sequencing — **[r2] replaces r1**

Feature breadth is done in demo mode (see PARITY.md). The path to
real-server parity, in order: adapt the app's pin client to the shipped
sync feed (cursor + watermark + tombstones + client `uuid`) → token auth
for `/media/` server-side → `flutter_appauth` OAuth2 integration → E2EE
fixture test, then api-mode E2EE enrollment/unlock (§6 endpoints) → camera
capture + workmanager background sync → push client (UnifiedPush, ntfy
dispatcher, FCM flavor) → map bake-off decision → remaining BACKEND_CHANGES
domains as they ship → store/F-Droid packaging.
