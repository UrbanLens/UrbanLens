# Spark audit — Python codebase audit progress

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

Started: 2026-10-06. Scope: all 2,977 `**/*.py` files (counted via
`python3 -c "import glob; ..."` from repo root).

Next free problem id at start: `P322` (see `docs/INDEX.md` header).

## Shards (no file in two shards; a file is done when its shard row is done)

| shard | scope | files | status | audited-by | findings |
|---|---|---|---|---|---|
| A | `bin/` (37) + `src/urbanlens/core/` (50) + root-level `*.py`, `src/urbanlens_ai/` (10), `tests/` root, `src/bin/` | ~120 | done 2026-10-06 | explore subagent + coordinator spot-check | P322, P323, P324, P325 filed; leads below |
| B | `src/urbanlens/dashboard/models/` (323) | 323 | done 2026-10-06 | explore subagent + coordinator spot-check | P326, P327, P328, P333 filed; leads below |
| C | `src/urbanlens/dashboard/services/` (627) | 627 | done 2026-10-06 (50+ files opened + shard-wide greps; not every file read line-by-line) | explore subagent + coordinator spot-check | P332 filed; leads below |
| D | `src/urbanlens/dashboard/controllers/` (100) + `forms/` + `middleware` + `tasks` + `plugins` + `management/` + rest of `dashboard_other/` | ~300 | done 2026-10-06 (controllers/forms/middleware/consumers/urls covered; `tasks.py` sampled, `plugins/` lightly) | explore subagent + coordinator spot-check | P329, P330 filed; leads below |
| E | `src/urbanlens/dashboard/tests/` (1518) + `migrations/` (65): coverage mapping | ~1583 | done 2026-10-06 (mapping + sampling, per protocol) | explore subagent + coordinator spot-check | P331 filed; F4/F5/F7 confirmed as P50/P316 territory, not re-filed |

## Protocol for each batch (so the next batch picks up without repeating work)

1. Read every file in the shard (for E: list + sample + grep for `skip`, `xfail`, `TODO`, empty `test_` bodies).
2. For each issue record: file:line, what is wrong (bug / missing test / deprecated / dead code / security / perf / typing), severity, evidence (quote or command output).
3. Before proposing a new `P#`, `grep` `docs/PROBLEMS.md` and `docs/archive/PROBLEMS-ARCHIVE.md` for the file/topic to avoid duplicates.
4. Return findings to the coordinator; only the coordinator writes `docs/PROBLEMS.md` + `docs/INDEX.md` (id allocator — avoids collisions).
5. Mark shard row done here with commit hash.

## Log

- 2026-10-06: tracker created, 2,977 files counted, shards defined. Batches A–E dispatched in parallel.
- 2026-10-06: all shards reported. Coordinator spot-verified every filed claim by re-reading the cited code (one claim narrowed: P329's assistant side — the single-flight turn lock bounds AI spend, so the entry records request/DB/render volume, not LLM cost; one claim narrowed: P330's media-copy side — misses are throttled, so the entry records the auth inconsistency, not unthrottled work). Filed P322–P333 (12 entries) in `docs/PROBLEMS.md` + rows in `docs/INDEX.md`; next free id is now `P334`. `bin/check_docs_index.py` passes (exit 0).
- 2026-10-06 (batch 3): services/ deep audit in 6 shards (apis 115, media/import/photos 56, ai/search 47, core/security/auth/sandbox 90, pins/locations/trips/wiki/geo/map/places/device_scan 135, tasks.py + plugins + 16 small dirs). Coordinator spot-verified every filed claim by re-reading the cited code. Filed P346–P363 (18 entries); next free id `P364`; checker passes.
- Subagent-verified but UNFILED (real, low severity — batch 4 candidates, evidence pointers kept): legacy Google gateways omit explicit `timeout=` on 9 calls (mitigated by `_RateLimitedSession` (5,30) default — `apis/locations/google/places.py:50,100,128,166,186,198`, `maps.py:436,556,604`); HIBP validator fails open on API outage (documented tradeoff — `validators/password.py:82`, `hibp.py:68-72`); USGS login bare `except Exception` + TODO, proceeds unauthenticated (`apis/locations/usgs.py:59-79`); unbounded `while True` pagination in calendar/photos-github clients (`apis/calendar/google.py:266`, `apis/photos/google.py:268`, `apis/infra/github/contributors.py:88`; cf. immich `_MAX_LIBRARY_PAGES` pattern); `route_import` has no provider-client test (`route_import.py:19-59`); `wants_tile_copy` Pillow decode without `@untrusted_parse` (`media/remote_copies.py:233-238`); import `metadata.json` coordinates stored with no range validation (`import_data.py:1571-1623`, cf. `images.coerce_coordinates`); global-search fallback double fan-out + trip-comment per-term list materialization (`global_search/engine.py:107-125`, `providers.py:902-906`); no memoization on vision/geocode-per-row/web-search exact-match cache (`ai/vision.py:139-201`, `ai/document_import.py:431-481`, `search/pin_web_search.py:96-100`); AI tests pin `<USER_DATA>` formatting while stubbing the gateway (no wire-shape assertions); trip slug suffix is MT `random` over 90k (`core/slugs.py:42-44`); API-key unknown-vs-legacy prefix timing oracle (`auth/api_keys.py:192-200` vs `:124-130`); enrichment density-score N+1 spatial counts (`locations/enrichment.py:398-420`, background); scan ingestion per-device `get_or_create` in request path (`device_scan/ingestion.py:69-70`); Stripe sync retry without backoff on RateLimitError (`tasks.py:5055-5056`); CRIS extraction soft-only limit, no retry (`tasks.py:1571`); DM geocode bare-except swallows retryable failures (`messaging/dm_location_detection.py:274`); floorplan per-row bare saves in loops (`floorplans/serialization.py:388,406,448`); fact-recompute bare `save()` can clear concurrent `needs_recompute` (`facts/confidence.py:234` vs `facts/evidence.py:142-145`).
- 2026-10-06 (batch 4): all of the above confirmed by coordinator greps/reads and filed as P364–P374 (11 grouped entries); next free id `P375`; checker passes. Bank is empty — every verified lead from batches 1–3 is now either filed (P322–P374, 53 entries) or refuted on record. Remaining unaudited-at-depth: `dashboard/models/` beyond pattern sweeps (323 files, shard B was grep-led), `dashboard/controllers/` files not individually read, `dashboard/tests/` per-file quality (PL6's territory), TS/SCSS frontend, `migrations/` past the noop guard.

## Deferred leads for the next batch (reported by subagents, NOT independently verified — re-measure before filing)

- Shard A: `urbanlens_ai/schema.py:99-110` `InferenceRequest.max_tokens` has no lower bound (`0`/negative passes schema + policy); `core/testing_network.py` `start()` patches `connect`/`sendto` but not `getaddrinfo`, so DNS still leaks (archive records only the `connect_ex` gap as fixed); `core/tests/slow_servers.py:72-82` writes a throwaway TLS key at default 0644; `bin/run_codeql.py:177-183` dead arm64 branch (both arms return the osx64 bundle); `bin/map_layers.py:7-9` wrong usage string + untyped `main()` + untested `unique_path`/`convert_image`; `src/bin/utils/settings.py:61` literal-`f` typo + `yaml.load(..., Loader=SafeLoader)` instead of `safe_load` + diverging placeholder constants vs `src/bin/settings.py`.
- Shard B: `Image.embedded_keywords` (nullable `fail_soft` JSON, same null-degradation path as `exif_data`, not named in `DATA_ENCRYPTION.md` Follow-up #4); `NotificationLog` has DM/group-message FKs for redaction but no safety-chat FK while `SafetyCheckinMessage.body` is plaintext; `SafetyCheckinPartner` still uses deprecated `unique_together` (`safety/model.py:584`); device-scan `DeviceSignalReading`/`DeviceScanEntry` have no indexes on hot FKs (`device_scan/model.py:180-203`); `cache/signals.py:68,121` bare-`except Exception` around cross-model name-refresh writes; pervasive `null=True` on string fields (~60 sites, NULL-vs-`""` ambiguity).
- Shard C: AI gateway error logs capture full prompt queues/responses (`ai/gateway.py:370-372,454`) and `global_search/engine.py:161-164` logs `parsed.raw` — none match the coordinates-in-logs grep, privacy review wanted; `media/held_upload.py:229-232` unbounded `handle.read()` on icon held-uploads (avatar path has a ceiling, icon paths do not); per-cluster bare `marker.save()` in `device_scan/clustering.py:254-267` (P277-adjacent, consider as sub-item not new entry).
- Shard D: safety-contact token POSTs (`SafetyContactMarkSafeView`, `SafetyContactOptOutView`, `SafetyCheckinMessageView` contact route) have no throttle on anonymous capability-URLs; WebSocket credential passed as `?key=` query string (`websocket_auth.py:63-68`); `HealthController` sets `throttle_classes = []` on DB/cache-touching probes; `WelcomeOnboardingForm.save()` whole-row save + `EditProfileView._save_profile` writes all form fields — both probable P5 instances, confirm against P5's table before filing.
- Shard E: `immich_form` (0 refs) and `street_imagery` plugin (0 refs) edge coverage; floorplan (15), billing (27), consensus/e2ee (30 each) thinnest areas — fold into PL6 batches, do not file separately; F4/F5/F7 are P50/P316 territory (schedule the documented `--shuffle` full run with seed recorded).
