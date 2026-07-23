# UrbanLens Companion App — Language & Technology Stack

Decided 2026-07-23. Android ships first; iOS and desktop follow from the same codebase. Implementation gated on the de-risking spike below.

Context

UrbanLens needs native clients: Android, iOS and desktop planned (talking to `/api/v1/` grown from dashboard/external_api/). Requirements that shaped the choice: the map is the core interaction (~8k pins today, clustered); field capture happens where connectivity doesn't (offline is a requirement, not a feature); E2EE messaging must interoperate byte-for-byte with the web client; the project is AGPL-3.0, privacy-first, and built by a solo developer.

Decision: Flutter + Dart

One codebase for Android, iOS, Windows/macOS/Linux. Alternatives rejected:

- Native Kotlin + Jetpack Compose — best single-platform result, but three platforms means two more rewrites for a solo dev.
- Kotlin Multiplatform / Compose Multiplatform — viable logic sharing, but no mature cross-platform map component; per-platform MapLibre glue on a map-centric app defeats the point.
- React Native — desktop support is fragmented out-of-tree forks; TS familiarity wasn't worth that.

Map architecture — abstraction first, MapLibre primary

A `MapView` abstraction is the day-one design, with platform-selected implementations behind it; no single Flutter map widget serves all targets well.

- Mobile: MapLibre GL (the `maplibre` Flutter plugin). Native GPU vector rendering; clustering computed in the native layer (supercluster-class — comfortably 10–100x current pin counts, unlike pure-Dart clustering); built-in offline region downloads.
- Desktop: `flutter_map` (pure Dart, runs where MapLibre's Flutter bindings don't; desktop is a browse/plan surface with CPU headroom, so Dart-side clustering is acceptable there).

The spike benchmarks both against the real exported pin dataset before the abstraction's API freezes.

Pin delivery — offline-first local sync, not tiles

Personal pins sync in full to a local drift (SQLite) DB and render from a local GeoJSON source with native clustering. This mirrors the web map's deliberate download-all strategy (map_pins_json cursor paging + Leaflet.markercluster, controllers/maps.py) and is forced by the field-capture use case regardless of rendering performance: the personal dataset must live on-device. API implication, decided before any Dart client is generated: a delta-sync endpoint — cursor-paged with `modified_since` and deletion tombstones, patterned on the existing map_pins_json cursor + map.pins.meta last_updated invalidation.

Server-side MVT (ST_AsMVT / django-vectortiles) is deferred to the community-locations layer — the genuinely unbounded dataset where tiles are correct. Prerequisite when it comes: pin/location bbox queries are currently raw lat/lng comparisons and must move onto PostGIS geometry.

Offline pin creation — outbox, not just a cache

drift doubles as a write-ahead outbox. Pins created offline get a client-generated UUID at capture (idempotency key the API must accept); an outbox table holds pending mutations (pending/inflight/failed) retried by `workmanager` with backoff; Location identity (lat/lng dedup) resolves server-side per existing Location semantics; pin field edits are last-write-wins on `updated`; photo uploads are resumable and ordered after their pin's create succeeds. Capture never blocks on the network.

Push — UnifiedPush/ntfy default, FCM as an optional flavor

Default transport is UnifiedPush against a self-hostable ntfy server — consistent with the AGPL/self-hosted ethos, the existing Gotify admin-alert precedent (services/notifications.py), and an F-Droid path with no Play Services dependency. An optional `play` build flavor adds FCM for a Play Store build; one dispatch interface, two transports. Server-side is net-new either way: device registration model + dispatcher alongside services/notification_delivery.py.

E2EE interop — exact targets

The Dart client reimplements frontend/ts/shared/e2ee-crypto.ts via `sodium_libs` (same libsodium): Argon2id (`crypto_pwhash`, ALG_ARGON2ID13, opslimit=2, memlimit=64 MiB, 16-byte salt, 32-byte output — but read pinned params from the key-bundle row, models/e2ee/key_bundle.py, never hardcode); two derivations (authKey from AccountKdf.auth_salt, wrapKey from password_wrap_salt); X25519 identity keys; XSalsa20-Poly1305 secretbox (24-byte nonce ‖ ciphertext) for key wrapping and messages; crypto_box_seal for conversation-key distribution; standard base64 with padding. docs/e2ee.md notes native apps with pinned code close the malicious-server gap web delivery can't — the app is a security upgrade if done exactly right, and silently broken logins if not.

Auth

OAuth2 + PKCE via django-oauth-toolkit (per split-architecture.md), `flutter_appauth` + `flutter_secure_storage` on-device. Per-platform redirect wiring is scoped work: Android App Links (custom scheme + assetlinks.json), iOS Universal Links later, loopback redirect on desktop. Prototyping uses the existing hashed-PAT bearer scheme (dashboard/external_api/) unchanged.

Licensing

The client is AGPL-3.0, matching the server; as sole copyright holder the owner can grant app-store exceptions where store terms conflict. Consequence: no third-party GPL/AGPL dependencies in the client (that freedom is only preservable for code we own) — flutter_map_tile_caching (GPL-3.0/commercial) is explicitly excluded; MapLibre offline regions cover mobile, and desktop tile caching if needed is a small dio+drift layer.

Client stack

| Concern | Choice |
|---|---|
| Map (mobile / desktop) | `maplibre` / `flutter_map`, behind shared `MapView` |
| State | Riverpod |
| HTTP | `dio` + OpenAPI-generated client (drf-spectacular server-side) |
| Models | `freezed` + `json_serializable` |
| Local DB / outbox | `drift` |
| Background sync | `workmanager` |
| Auth | `flutter_appauth` + `flutter_secure_storage` |
| WebSockets | `web_socket_channel` (ws/notifications/, ws/messages/) |
| E2EE | `sodium_libs` |
| Camera/GPS | `camera`, `geolocator` (mobile only in v1) |
| Push | UnifiedPush/ntfy default; FCM `play` flavor |

Backend prerequisites

Grown from dashboard/external_api/ over the service layer, per docs/external_app_api_plan.md and docs/api-expansion-candidates.md:

1. Versioned `/api/v1/` delta-sync endpoints for pins/maps/locations (cursor + modified_since + tombstones), reusing services/map_pins/ (MapPinPayloadService, MapPinCache).
2. Client-generated pin UUIDs accepted on create (outbox idempotency).
3. OAuth2+PKCE (django-oauth-toolkit).
4. Authenticated media — nginx serves /media/ with no auth check (config/nginx/django.conf); must be fixed before the app fetches photos (also flagged in split-architecture.md).
5. Push device registration + ntfy dispatcher in services/notification_delivery.py.
6. drf-spectacular OpenAPI schema driving Dart client generation.
7. Deferred: MVT tiles for community locations (needs PostGIS-geometry bbox queries first).

De-risking spike (gates everything above)

- Map bake-off: real exported pin dataset rendered in both MapLibre (native clustering) and flutter_map (Dart clustering) on a mid-range Android device; frame times while panning/zooming. Freezes the MapView abstraction with data.
- E2EE interop: fixture generated by the web client (salts + wrapped private key) derived and unwrapped from Dart. Byte-exact or bust.
- Desktop build: the same spike project runs the flutter_map implementation on Windows unchanged.
- API round-trip: PAT auth against whoami + POST pins/, including one airplane-mode capture → reconnect → outbox sync.

Sequencing after the spike: delta-sync read API + map screen with local cache → field capture (camera+GPS → outbox) → OAuth2 migration + authenticated media/photos → trips, wiki, chat/E2EE, push, desktop target.
