# X24 — The map request's redundant SQL is app code defeating Django's caches, and one template tag is 28% of it

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

`id: X24` · `type: experiment` · `status: holds` · `updated: 2026-09-16` · `source: probe via connection.execute_wrapper with urbanlens-filtered stack capture plus per-processor timing wrappers, run in urbanlens_development_main_test_runner, settings=urbanlens.UrbanLens.settings.test, UL_TEST_DB_NAME=ul_profile_probe --reuse-db, real django.test.Client GET of map.view with force_login, session of 2026-09-16 (scratch probe, not preserved)`

This is an independent same-day probe of the request [X23](template-engine-share-measured.md)
measured, run to get a per-query and per-processor attribution X23 did not have. It also produced
the clean uninstrumented wall-time baseline that revealed X23's denominator did not reproduce; that
correction is recorded in X23 itself, not here. Read X23 first for the template-engine side of this
request; this record covers the SQL and context-processor side.

## Environment, all confirmed at runtime

`bin/run_tests.sh --verify-only` reported tree match at md5 `1dbbe19aeafe5c720c297958b5c1bcb2`.
`header.html` was byte-identical host and container at md5 `aca89424af9d9f543d5a93583895910c` /
19,633 bytes — the 19,937-byte stale-container drift X23 recorded was gone by this session.
`settings.DEBUG` and `engine.debug` both `False`; loaders `['django.template.loaders.cached.Loader']`.
PostgreSQL 17.5. Measured user id 14 — `view_site_admin=False`, no groups, not staff or superuser,
0 pins — with a throwaway user id 13 created first to absorb the bootstrap site-admin promotion (log
confirmed the promotion hit the throwaway, not the measured user). Response 200, 239,501 bytes,
byte-identical on every run. Host load 2.89 at start, 2.84 at end, on 8 cores.

## Queries: 24 total, 14.28 ms — 15 queries / 8.14 ms in view+middleware, 9 / 6.15 ms during template render

Duplicate shapes, and the finding that **every duplicate comes from a different line — never the
same line twice**:

| n | total ms | each | issuer |
|---|---|---|---|
| 3 | 3.68 | 1.24, 1.30, 1.14 | `dashboard_profiles` — 3 different lines |
| 2 | 2.80 | 0.77, 2.04 | permissions ⋈ `auth_group_permissions` — 2 different lines |
| 2 | 1.01 | 0.45, 0.56 | permissions ⋈ `auth_user_user_permissions` — 2 different lines |

Singles worth naming: `safety_checkins` 0.88 ms (`context_processors.py:312`), `auth_user` 0.59 ms
(`middleware.py:247`), `pin_lists` 0.54 ms (`maps.py:191`), `labels` 0.49 ms (`maps.py:179`), map
centre 0.44 ms (`profile/model.py:818` `compute_map_center`), subscription rows 0.41 ms / 0.40 ms
(`subscriptions/model.py:278` and `:320`), `site_settings` 0.38 ms (`queryset.py:33` `get_current`).
The remaining 9 are single 0.20-0.33 ms queries from `maps.py` lines 177, 184, 185, 204 and the
three nav context processors.

**Django's own machinery is not at fault here.** Session load and the auth user fetch each run
exactly once, and `ModelBackend`'s permission cache demonstrably works: the `add_dev_toolbar`
processor calls the same `show_dev_admin_features` later in the request for **zero** queries. It is
app code, not the framework, that defeats the caches below.

### The three profile fetches

- `middleware.py:248` `WriteSourceMiddleware.__call__` → `user.profile` — correctly uses the
  related-object descriptor cache, no query.
- `controllers/maps.py:174` `view_map` → `Profile.objects.get_or_create(user=request.user)`
- `templatetags/dashboard_tags.py:469` `assistant_enabled_flag` →
  `Profile.objects.get_or_create(user=user)`

`get_or_create` always issues a `SELECT` regardless of the descriptor cache the middleware already
populated — the manager method has no way to know the cache exists, so the second and third call
sites each pay for a lookup the first one already did.

### The four permission queries

Django's own `get_all_permissions` is a 2-query pattern (user perms, then group perms), correctly
cached per `User` instance. It runs **twice** here because **two different `User` instances exist
in one request**:

- View phase: `maps.py:194` → `show_dev_admin_features` → `has_perm` on `request.user` → 2 queries,
  then cached on that instance.
- Template phase: `dashboard_tags.py:470` → `assistant_available` → `ai/access.py:27`
  `user_has_feature(profile.user, ...)`, where `profile` came from the third `get_or_create` above
  and so carries no cached `user` FK. `profile.user` fires a second `auth_user` `SELECT` (0.30 ms),
  yielding a **fresh** `User` instance with an empty `_perm_cache`, and 2 more permission queries.

**Headline: `{% assistant_enabled_flag %}` alone issues queries 16-19 — 4 of 24 queries and 4.04 of
14.28 ms of SQL, 28% — to re-derive a profile and a user the request was already holding.**

## Per-processor deferred cost, 5 runs, medians

Stable across all 5 runs: 24 queries and 14 `Deferred` key setups every run; wall 57.00-75.32 ms
(median 63.65), SQL 17.52-21.85 ms (median 18.74), setup median **7.14 ms** (reproducing X23's
7.74 ms), bind 0.18 ms. Every processor bound exactly once.

| processor | total ms | bind | compute | queries | keys read/offered |
|---|---|---|---|---|---|
| `add_direct_messages` | 2.976 | 0.009 | 2.967 | 3 | 2/2 |
| `add_active_checkins_banner` | 2.526 | 0.009 | 2.518 | 1 | 1/1 |
| `add_unread_notifications_badge` | 1.118 | 0.007 | 1.111 | 1 | 1/1 |
| `add_comment_map_config` | 0.195 | 0.008 | 0.187 | 0 | 1/1 |
| `add_environment_indicator` | 0.097 | 0.009 | 0.088 | 0 | 2/2 |
| `add_dev_toolbar` | 0.092 | 0.016 | 0.077 | 0 | 2/3 |
| `add_feature_access` | 0.074 | 0.018 | 0.056 | 0 | 1/7 |
| `add_site_settings` | 0.058 | 0.028 | 0.030 | 0 | 2/3 |
| `add_pending_account_deletion` | 0.025 | 0.011 | 0.014 | 0 | 1/3 |
| `add_keyboard_shortcuts` | 0.019 | 0.007 | 0.012 | 0 | 1/1 |
| `add_distance_units` | 0.009 | 0.009 | 0.000 | 0 | 0/1 never read |
| `add_unread_messages_badge` | 0.007 | 0.007 | 0.000 | 0 | 0/1 never read |
| eager: `add_page_name` 0.008, `csrf` 0.008, `auth` 0.005, `messages`/`add_demo_context` 0.004, `debug`/`request` 0.002/0.001 | | | | | |
| **TOTAL** | **7.228** | **0.171** | **7.058** | **5** | |

Three processors are **94% of the deferred cost** (6.62 of 7.06 ms) and issue all 5 of the context
processors' queries. Their compute time far exceeds their SQL — `add_active_checkins_banner` spends
2.518 ms of compute around a single 0.88 ms query — so roughly 4.5 ms of the 7.06 ms is Python, not
the database. `add_feature_access` computes for `show_games_nav` alone (`ALPHA_FEATURES`) while its
other six flags (`AI`, `PLACES`, `SEARCH`, `VIDEO_UPLOADS`, `DOCUMENT_UPLOADS`, `BETA_FEATURES`) go
unread on this page. `add_distance_units` and `add_unread_messages_badge` are computed and never
read at all — pure waste, not attribution error, since `compute` is 0.000 for both.

## Confidence

**HIGH** on composition, ratios and causal attribution: fully deterministic across runs (24
queries, 14 setups, identical call sites, byte-identical responses), and the causal chains
(duplicate profile fetches, the two-`User`-instances permission split) were read directly off
captured stacks, not inferred from timing alone.

**MODERATE** on absolute milliseconds. Distortions to weigh before citing an absolute number from
this record:

- 12% stdev on wall time (67.38 ms median, stdev 7.83); shared 8-core host at load ~2.8, and **a
  concurrent session was using the same container and the same `ul_profile_probe` database during
  this run**.
- Everything ran inside one outer rolled-back `transaction.atomic()`, so no per-request
  `BEGIN`/`COMMIT` was captured — production pays those on top of the figures here.
- Test settings strip `django_prometheus` middleware, so production carries middleware this probe
  did not measure.
- `setup_test_environment()` was deliberately not called, so there is no per-render signal
  instrumentation running underneath these numbers — closer to production than a pytest run, but
  not identical to X23's conditions.
- Stack capture inflated the attribution run's own wall time (89.27 ms vs. the 67.38 ms clean
  median) but not per-query durations, which are timed around `execute()` only; the per-processor
  timings in the table above came from separate, lighter-weight wrapper runs, not the stack-capture
  run.
- locmem cache, not Redis, though `get_current()` still issued a real query since its memo is
  request-scoped rather than cache-backed.

**Cross-check against X23**, same container and database and day, independent session: template-
phase SQL 6.15 ms here vs. 6.52 ms there; deferred setup 7.14 ms vs. 7.74 ms there — both agree to
within 8%. X23's 17.57 ms "outside any template" figure is well above this run's 8.14 ms
view+middleware figure; the most likely reason is that X23 used `CaptureQueriesContext`'s debug
cursor where this probe used `connection.execute_wrapper`, and the two were not cross-calibrated
against each other this session.

## What this does not establish

Nothing here measures a page other than `map.view`, an account other than a 0-pin user, or a host
other than this one.

The `assistant_enabled_flag` fetches were repaired immediately after this measurement — the tag now
reads `user.profile` and falls back to `get_or_create` only when no profile exists — but that fix is
verified by its own test, not by this record, and the request was **not** re-measured with it in
place, so treat the 28% as the cost that was there, not as a saving that has been demonstrated.

`maps.py:174` was repaired the same way immediately afterwards, and a test asserts the map request
now selects the profile row exactly once (it selected it twice at that point, the middleware's read
plus `view_map`'s own). That test pins the count, not a duration; no re-measurement of the request's
SQL time has been taken with either fix in place.

The wider scope is untriaged: `Profile.objects.get_or_create(user=...)` appears **286 times across
59 non-test files** (`controllers/trip.py` 33, `controllers/safety.py` 29,
`controllers/vault_photos.py` 17, `controllers/maps.py` 14). That makes it an idiom, not two
isolated call sites. It is not 286 defects — `get_or_create` is right where a profile may genuinely
be absent, and the `consumers.py` call sites take `self.scope["user"]` on a different lifecycle. The
defect is specifically re-deriving a profile the current request already holds, and which call sites
do that has not been established.
