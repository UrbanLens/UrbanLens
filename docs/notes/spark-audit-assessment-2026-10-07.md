# N48 — A weaker model's audit of 0.9.0 was re-read finding by finding: nothing high severity was confirmed, five medium findings went to fix PRs, and its P-ids are not UrbanLens problem ids

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

`id: N48` · `status: current` · `updated: 2026-10-07`

**What was assessed.** Branch `feat/spark_audit` at commit `b59e7d02a`, the output of a weaker model ("Muse Spark 1.3") that
audited the codebase and filed findings P322 to P406 plus a list of unfiled leads, against `release/v_0_9_0` at
`3f38a3fda`. Its findings were written against an older commit (about 2026-10-06), so line numbers had moved and a few
were already fixed. Five verifier agents re-read the cited code on 2026-10-07, one batch each, and returned a verdict per
id. They were read-only: no test was run and no deployment was touched.

**Result.** Nothing high severity was confirmed. Five medium findings went to fix PRs:

| Finding | What was wrong |
|---|---|
| Spark P340 | The AI gateway logged full prompt queues and responses, and global search logged the parsed query, against `redact.py`'s rule that pin names stay out of logs |
| Spark P341, P363 | An icon held-upload was read with no byte ceiling, as was a stored field read; both bounded by the upload cap only |
| Spark P395 | Request-driven amplification (provider import id lists, tools import/export, email invites, vote materialization, DM uploads); the part rated medium was the import's missing single-flight guard |
| Spark P351 | The import's `_read_json` read a JSON file with no size cap |
| Spark P376 | A missed check-in tick escalated with no final warning, and an email-scoped opt-out was not honoured when the same person was also a profile contact |

The low findings were fixed in a batch (the small-fixes PR, branch `fix/spark-small-fixes`), filed as issues, or dropped,
as the tables below say. The rest were refuted, not bugs, already fixed, or duplicates of open issues.

**Spark's ids collide with real ones.** Its `P322` to `P338` reuse ids the real series already used (`docs/PROBLEMS.md`
and the archive), and its `P339` to `P406` continue past the real series' end at `P338`, so a later reader will take them
for ids that exist. Do not cite a Spark id as an UrbanLens problem id. Cite it as "Spark P###", and name the issue where
one exists.

## Verdicts

Verdicts are the verifiers' words: `CONFIRMED` (real as described), `PARTLY` (some sub-claims real), `REFUTED` (the code
does not do that), `NOT-A-BUG` (accurate but intended or harmless), `DUPLICATE #N` (an open issue covers it). Severity is
the verifiers' (`high` is a security or privacy exposure, data loss or corruption, or a production crash on a normal
path; `medium` is wrong behaviour users would hit or a significant cost; `low` is robustness, dead code or a missing
test). "This PR" is the small-fixes PR above. Evidence is at the verifiers' line numbers against `3f38a3fda`, and was
not re-measured when this was written.

### Batch 1

| id | verdict | severity | disposition |
|---|---|---|---|
| Spark P322 | CONFIRMED | low | drop (`src/bin/app.py` dev helper); this PR removes its duplicated `__main__` block |
| Spark P329 | PARTLY | low | drop |
| Spark P333 | NOT-A-BUG | - | drop |
| Spark P337 | CONFIRMED | low | drop (`unique_together` style debt) |
| Spark P340 | CONFIRMED | medium | fix PR: log lengths or `redact_text()` at `ai/gateway.py:372,454` and `global_search/engine.py:163` |
| Spark P344 | NOT-A-BUG | - | drop |
| Spark P348 | NOT-A-BUG | - | drop (coordinates on a concealed wiki are by design) |
| Spark P351 | PARTLY | medium | fix PR: a per-JSON size cap on the import's `_read_json` (ceiling was about 20 GiB) |
| Spark P355 | CONFIRMED | low | this PR: the resend path never released its email reservation |
| Spark P359 | CONFIRMED | low | file an issue (trip visibility N+1, opt-in and capped at 100) |
| Spark P363 | CONFIRMED | low | fix PR with P341 (`stored_field.py` read with no size check, bounded only by the upload cap) |
| Spark P366 | PARTLY | low | drop |
| Spark P370 | PARTLY | low | drop |
| Spark P374 | REFUTED | - | drop |
| Spark P377 | CONFIRMED | low | this PR: `Article.editable_by` was dead code and is deleted |
| Spark P379 | PARTLY | low | this PR: the comment-reaction double-tap raised `IntegrityError` |
| Spark P385 | PARTLY | low | drop |
| Spark P388 | PARTLY | low | file an issue (`email_log` check-then-act) |
| Spark P392 | NOT-A-BUG | - | drop |
| Spark P396 | PARTLY | low | drop |
| Spark P398 | PARTLY | low | drop (`markup.py:390` anonymous view has no throttle) |
| Spark P403 | PARTLY | low | file an issue; same class as #293 (migrations importing live code) |

### Batch 2

| id | verdict | severity | disposition |
|---|---|---|---|
| Spark P323 | PARTLY | low | drop (only the `BACKUP_DIR` part is real; a dev CLI has no trust boundary) |
| Spark P328 | CONFIRMED | low | drop (search-history uniqueness is case-sensitive) |
| Spark P331 | REFUTED | - | drop (the OAuth authorize and introspect views are tested by URL) |
| Spark P334 | CONFIRMED | low | drop (`max_tokens` is unbounded, but only internal constants reach it) |
| Spark P338 | PARTLY | low | drop (device-scan tables have no indexes; the atomic-claim claim is refuted) |
| Spark P343 | CONFIRMED | low | file an issue (a WebSocket credential in the `?key=` query string; needs design) |
| Spark P346 | PARTLY | low | drop |
| Spark P352 | DUPLICATE #287 | - | add `fetch_panel_source` and its siblings to #287's roster |
| Spark P356 | CONFIRMED | low | this PR: uncapped `.content` at five sites now goes through `read_capped` |
| Spark P360 | PARTLY | low | file an issue (device-scan recompute per repeated MAC; boundary vote resolved inline) |
| Spark P365 | NOT-A-BUG | - | drop |
| Spark P369 | PARTLY | low | drop |
| Spark P373 | PARTLY | low | drop |
| Spark P378 | PARTLY | low | file an issue (`location_mention` syncs in `save()` only; latent) |
| Spark P382 | PARTLY | low | drop (a DM-attached markup map could be reattached as a check-in route map; noted) |
| Spark P386 | CONFIRMED | low | drop |
| Spark P390 | CONFIRMED | low | drop (stale comment at `key_bundle.py:56-58`) |
| Spark P393 | PARTLY | low | drop (Immich `last_verified` is written once) |
| Spark P397 | PARTLY | low | this PR fixes the saved-filter icon, profile name and bbox 500s; not done: the memories future-date check (the feature docs do not say memories are past-only) and labels convert-parents |
| Spark P402 | PARTLY | low | drop, but check the unbatched DELETEs in 0.9.0 migrations 0038/0042/0046/0047 before the deploy, given `dashboard_location_cache` is 81% of production's database (#290) |

### Batch 3

These verdicts are taken from that verifier's final report, and were not re-read when this note was written.

| id | verdict | severity | disposition |
|---|---|---|---|
| Spark P324 | CONFIRMED | low | drop (`src/bin/research.py` is dead: the script's own URL settings are placeholders); this PR deletes it |
| Spark P327 | CONFIRMED | low | drop (the only production caller is `record_detail_result`; concurrent writers carry the same upstream facts) |
| Spark P330 | PARTLY | low | file an issue (`RemoteImageCopyView` is a plain `View` while `remote_tiles` has `LoginRequiredMixin`); the `/metrics` half is refuted, since `dashboard.E006` refuses to start production with it open |
| Spark P335 | CONFIRMED | low | the DNS half: this PR makes the test network guard refuse hostname lookups; the world-readable key is in a 0700 `mkdtemp` directory, test-only, and was dropped |
| Spark P339 | CONFIRMED | low | drop (convention drift on nullable string columns; the encrypted five are already tracked) |
| Spark P342 | NOT-A-BUG | low | drop (the token is a 122-bit UUID and a replay does not notify) |
| Spark P347 | NOT-A-BUG | low | drop (`docs/FEATURES.md:850`: AI is logged, not capped) |
| Spark P350 | CONFIRMED | low | file an issue (`videos.py` clears only container-level location tags; no stream-level tag was seen in practice) |
| Spark P354 | PARTLY | low | drop (the outbox race is refuted by `beat_lock`; the breaker and WebAuthn races are real but narrow) |
| Spark P358 | DUPLICATE #265 | low | drop |
| Spark P362 | NOT-A-BUG | low | drop (served PDFs carry `nosniff` and a `default-src 'none'` CSP on the media origin) |
| Spark P367 | CONFIRMED | low | drop (runs in the sandbox queue on bytes the server just re-encoded; only the guard's logging is missing) |
| Spark P371 | NOT-A-BUG | low | drop |
| Spark P375 | PARTLY | low | `Friendship.block` has no production caller (`block_profile` is the live path); this PR deletes it. The `PENDING` half is legacy rows, covered by #295 |
| Spark P380 | PARTLY | low | drop (the unique-constraint races are refuted: `get_or_create` re-gets on `IntegrityError`; the one real half has no production caller) |
| Spark P383 | PARTLY | low | drop (latent; an XOR check on a message's sender would break profile deletion) |
| Spark P387 | PARTLY | low | drop |
| Spark P391 | DUPLICATE #271 | low | drop |
| Spark P395 | PARTLY | medium | fix PR: `ImportStartView` has no single-flight claim where `ExportStartView` has one. Of the other five sub-claims: the field-count one is capped near 1000 by Django's default, the email invites call `email_rate_limit_error`, DM uploads reserve quota, and provider downloads go through `fetch_with_revalidated_redirects`; one is real but low |
| Spark P400 | PARTLY | low | this PR fixes the two worth a small fix: a negative `purge_demo_accounts --ttl-hours`, and `anonymize_stored_photo_filenames` moving the file before the database update. The other three (redirect registration, the integration tools' stdout) were not changed |
| Spark P405 | REFUTED | - | drop (`del` on an absent header does not raise; the DRF parsers already refuse NUL). Checked on Django 5.1.1, not the pinned 6.0.8 |
| Spark P406 | PARTLY | low | this PR fixes the `checks.py` private-range check (it stopped at `172.20.`). The whitespace-as-symbol password count, the unguarded `float()` in `redata_media_gateway.py` and the `source="takeout"` test-data nit were not changed; the group-thread claim is refuted |

### Batch 4

| id | verdict | severity | disposition |
|---|---|---|---|
| Spark P325 | REFUTED | - | not a defect, but `CoreConfig` in `core/apps.py` is dead (not in `INSTALLED_APPS`); this PR deletes it |
| Spark P326 | PARTLY | low | drop |
| Spark P332 | CONFIRMED | low | this PR: CSV formula injection in both export writers |
| Spark P336 | CONFIRMED | low | drop (dev helper cosmetics) |
| Spark P341 | CONFIRMED | medium | fix PR: an icon byte ceiling like `AVATAR_MAX_UPLOAD_BYTES` on the held-upload paths |
| Spark P345 | DUPLICATE #265 | - | drop |
| Spark P349 | NOT-A-BUG | - | drop |
| Spark P353 | NOT-A-BUG | - | drop |
| Spark P357 | REFUTED | - | drop |
| Spark P361 | NOT-A-BUG | - | drop |
| Spark P364 | PARTLY | low | this PR bounds the Google Photos picker listing; `github/contributors.py:88` has the same unbounded loop and was not changed |
| Spark P368 | CONFIRMED | low | this PR: an imported photo's NaN or out-of-range coordinates |
| Spark P372 | PARTLY | low | file an issue (bounded N+1s) |
| Spark P376 | PARTLY | medium | fix PR (safety check-in: the final-warning window and the email-scoped opt-out) |
| Spark P381 | PARTLY | low | drop (`GooglePlace` has no TTL; whether Google's caching terms need one was not checked) |
| Spark P384 | PARTLY | low | drop |
| Spark P389 | PARTLY | low | drop |
| Spark P394 | PARTLY | low | file an issue (unthrottled region-search, thumbnail, billing and calendar-import routes; see #278); this PR also answers a spent Nominatim budget in region search and holds calendar import to `max_upcoming_trips_per_user`, both found while triaging it |
| Spark P399 | REFUTED | - | drop (only `MarkupMapShareDetailView` is untested) |
| Spark P401 | CONFIRMED | low | drop |
| Spark P404 | PARTLY | low | this PR: `base.py`'s `_env_bool` read a typo as False, so `SESSION_COOKIE_SECURE=ture` disabled secure cookies |

### Batch 5: unfiled leads

Leads Spark reported but did not file, with ids of their own (`A-2`, `U-17`, and so on).

| id | verdict | severity | disposition |
|---|---|---|---|
| Spark A-2 | CONFIRMED | low | this PR: `testing_network` did not patch `getaddrinfo`, so test DNS lookups left the machine |
| Spark C-1 | PARTLY | low | same as P340: the AI gateway logs prompts on its error path; in the logging fix |
| Spark C-2 | CONFIRMED | low | global search logs the raw query; in the logging fix |
| Spark D-2 | CONFIRMED | low | file an issue (the same as P343) |
| Spark U-3 | CONFIRMED | low | this PR: the USGS M2M login swallowed every exception and went on unauthenticated |
| Spark U-7 | CONFIRMED | low | this PR (the same as P368) |
| Spark U-14 | CONFIRMED | low | file an issue (device-scan ingestion does a `get_or_create` per device, about 200 a scan) |
| Spark U-17 | CONFIRMED | low | this PR: `dm_location_detection` caught `Exception`, defeating the task's `autoretry_for=(OSError,)` |

Spark's lead C-3 observed that `hold_upload` callers check size against the site cap (250 MB) only, which is why P341 is
medium rather than low; it carries no verdict of its own. The verifiers also noted an incidental in
`dm_location_detection.py`: the DM address candidate was logged at WARNING, handled in the logging fix. Everything else
the verifiers saw in the unfiled leads was refuted, not a bug, or too trivial to track.

## What was not verified

- Batch 3's verdicts were not re-read after the verifier returned them. Three of its own caveats stand: P405 was checked on
  Django 5.1.1 rather than the pinned 6.0.8, P330's `RemoteImageCopy` sources were not all traced (a private-source URL would
  raise the severity), and P350's severity depends on whether any real camera writes location only to stream tags.
- P358 and P391 were mapped to #265 and #271 by title; confirm the scope matches.
- None of the verdicts was re-measured after 2026-10-07, and none was run against a deployment.
- `P322` to `P338` appear both as Spark ids and as real former problem ids; every row here is Spark's, and none of
  them says anything about the real problem with that number.
