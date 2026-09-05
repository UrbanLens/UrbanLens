# INDEX

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

One line per record, never wrapped, so a single `grep` returns a complete
answer. Read this before reading anything else in `docs/`.

```bash
grep -E '^\| P12 ' docs/INDEX.md      # one record by id
grep -i 'encryption' docs/INDEX.md    # by keyword
grep -E '\| open ' docs/INDEX.md      # everything still open
```

**Next free id:** `P74` · `T3` · `PL7` · `D6` · `X14` · `I4` · `R26` · `N7`

Ids are allocated here and never reused or renumbered. Add the row in the same
commit as the entry, so a duplicate id becomes a merge conflict rather than a
silent collision. `docs/README.md` has the record types, the status values for
each, and the house style. `P` numbers entries inside `PROBLEMS.md`; every other
prefix numbers a whole document.

Resolved problems are **not** listed here — they move to
[`archive/PROBLEMS-ARCHIVE.md`](archive/PROBLEMS-ARCHIVE.md), which holds 288
of them. This index is what is live. Grep the archive before concluding a defect
is new - and an archived entry keeps its `id:` line, so a citation of `P70`
still resolves after it is fixed, and the id is never handed out again.

| id | status | updated | claim | path |
|---|---|---|---|---|
| P1 | open | 2026-09-01 | VirusTotal scanning is hash-lookup-only, so a file VirusTotal has never seen falls back to ClamAV forever | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P2 | open | 2026-08-31 | `parse_for_preview` parses archives and KML in the request, blocking `UL_UNTRUSTED_PARSE_POLICY=deny` | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P3 | open | 2026-08-31 | The pin-detail hero no longer links to `PinRelinkView.get`, orphaning the `pin.link` wiki picker | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P4 | open | 2026-08-31 | `urbanlens_development_main_test_runner`'s venv is missing five dev deps, silently dropping coverage | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P5 | open | 2026-08-25 | Dialog forms post every field and handlers save every column, so untouched values overwrite and re-attribute | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P6 | open | 2026-08-21 | Production REData still 404s `/api/v1/public-locations/`, so a fresh dev environment seeds no catalog pins | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P7 | open | 2026-08-19 | nginx pins its app upstream at config load and REData's `ref` is stored as permanent identity | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P8 | open | 2026-08-20 | `Friendship.unique_together` permits both `A->B` and `B->A`, so "one row per pair" is convention only | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P9 | open | 2026-08-19 | REData gaps remain - `?limit=` is inert, 15 routes unwired, and a `tile_template` slide is a single 256px tile | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P10 | open | 2026-08-19 | `main` is untested against an empty database; the multiple-leaf migration conflict that broke it is gone | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P11 | open | 2026-08-15 | ~40 raw `fetch()` calls bypass `fetch-json.ts` and fail silently; Organize's Media tab is unwired dead UI | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P12 | open | 2026-07-25 | ~10 SCSS files use undefined `--ul-accent`/`--ul-border`/`--text` vars, so dark mode never reaches those rules | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P13 | open | 2026-07-23 | Pin-detail external-data freshness is one site-wide `external_data_cache_days` knob, not per-source | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P14 | open | 2026-07-23 | Custom pin and label icons are readable by any authenticated user, and replaced icons strand files | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P15 | open | 2026-07-22 | openresty's 90s proxy cap cuts any Overpass query needing longer, whatever `[timeout:N]` asked for | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P16 | open | 2026-07-22 | Aliases and label membership are still strictly per-pin, with no aggregation across child pins | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P17 | open | 2026-07-24 | `docker compose exec app pytest` trips the localhost-only network guard because Valkey is a bridge IP | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P18 | open | 2026-07-25 | The setup wizard's always-dark sidebar uses inverting `--ul-grey-N` text tokens, unreadable in dark mode | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P19 | open | 2026-07-25 | Audit re-verification's residual gaps remain: dead ownership re-check, 1,100-line `_dark.scss`, stub AI gateway | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P20 | open | 2026-07-25 | The legacy-CID repair leaves the CID on the wrong `Location`, so `by_cid()` resolves it wrongly for everyone | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P21 | open | 2026-07-26 | `LocationWikiEditView.post` drops invalid wiki field edits and still answers `{"ok": true}` | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P22 | open | 2026-07-31 | REData's `/api/v1/parcels/lookup/` crash-loops gunicorn workers with OOM/WORKER TIMEOUT on chiron | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P23 | open | 2026-07-31 | The production celery worker's env sets `UL_SITE_URL=staging.urbanlens.org`, so built URLs point at staging | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P24 | open | 2026-08-05 | A campus pin aggregates only the nearest CRIS building's media, not the survey's full USN roster | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P25 | open | 2026-08-07 | `Comment.profile` CASCADEs but `TripComment.author` SET_NULLs, so account deletion erases only some comments | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P26 | open | 2026-08-07 | `create_group_message` never validates `key_version`, so a sender can use a key a removed member holds | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P27 | open | 2026-08-08 | Saved-filter regions use leaflet-draw's transactional remove tool, so deleted polygons resurrect on the next draw | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P28 | open | 2026-08-12 | The upload quota check is fail-open under a cache lock, so a bulk import's fan-out can still exceed the quota | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P29 | open | 2026-08-13 | 186 write routes have no test naming them; the smoke sweep proves only that they do not 5xx | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P30 | open | 2026-08-13 | Backups are plain-SQL with no restore path, and the repo's only `pg_restore` example cannot read them | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P31 | open | 2026-08-13 | Session and DM chat sockets have no rate limit and cap frame size only after the whole frame is parsed | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P32 | open | 2026-08-13 | `check_rate_limit` returns True on a `DatabaseError`, so a database failure uncaps paid-API spend | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P33 | open | 2026-08-13 | `Label.color` has no `save()`-level coercion, so a value bypassing form validation is stored unvalidated | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P34 | open | 2026-08-13 | 22,636 lines of inline template JS sit outside every automated check, with duplicated escaping helpers | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P35 | open | 2026-08-13 | Seven named routes still have no discoverable caller and remain unreviewed authorised surface | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P36 | open | 2026-08-13 | 45 BEM modifiers are applied in templates with no CSS rule, so intended visual states never render | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P37 | open | 2026-08-13 | 100 write handlers totalling 1,217 statements never execute under the test suite | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P38 | open | 2026-08-13 | `Pin.change_category`, `Pin.add_category` and `Wiki.add_category` have no production callers, so their tests fake coverage | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P39 | open | 2026-08-13 | `clean_color` coerces invalid colours to the default, so API clients lose the value silently instead of a 400 | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P40 | open | 2026-08-13 | `Pin.by_category` and `Wiki.by_category` have no callers and omit `distinct()`, so any caller inherits duplicate rows | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P41 | open | 2026-08-13 | 70 of 251 public queryset methods have no production caller, so their logic may be duplicated inline elsewhere | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P44 | open | 2026-08-16 | `isMouseContextMenu` misreads a keyboard context menu as touch, so the next Enter activation may be swallowed | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P46 | open | 2026-08-16 | A group message can still be sent under a key version a removed member holds | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P47 | open | 2026-08-16 | A deleted message's preview survives in the recipient's notification list | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P48 | open | 2026-08-17 | Logging out leaves every decrypted E2EE key cached in IndexedDB, and nothing clears it | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P49 | open | 2026-08-17 | `npm run git-squash` is a force-deploy with none of `deploy.sh`'s dirty-tree guards | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P50 | open | 2026-08-18 | `test_safety_chat` and `test_migration_0039_reverse` fail only under a randomized suite order | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P51 | open | 2026-08-22 | Native `<select>` popups stay light-on-light in dark mode despite `color-scheme: dark` | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P52 | open | 2026-08-24 | `.app-nav-right` runs 40px past a 390px viewport, so every page scrolls sideways at phone width | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P53 | open | 2026-08-24 | One Private Pin page load fires dozens of concurrent panel requests and can exhaust the DB connection pool | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P54 | open | 2026-08-23 | `docker-compose.hot-reload.yml` crash-loops when the checkout is not the container's uid | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P55 | open | 2026-08-23 | A community quota bonus survives un-sharing the photo that earned it | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P56 | open | 2026-08-28 | `Cross-Origin-Embedder-Policy` is unset, and the third-party host inventory needed to set it does not exist | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P57 | open | 2026-08-29 | The test-quality audit left ~15 findings unfixed, from an unpatched `connect_ex` guard to untested views | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P58 | open | 2026-08-31 | A photo's grid tile can 404/500 for seconds after upload while async processing renames its file | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P59 | open | 2026-08-31 | A `lightbox-associations.webp` thumbnail on the `ae97b86` dev account is durably broken, not just racing | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P60 | open | 2026-08-31 | `vault-photos.spec.ts`'s sort test can tie on a persistent dev DB because it relies on random captions | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P61 | open | 2026-08-31 | Vault album bulk delete, send-to-wiki and share render hidden forever, because only a `Pin` owner gets URLs | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P62 | open | 2026-08-31 | Video uploads are charged to quota but appear nowhere in the Vault | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P63 | open | 2026-08-31 | Adding a third Vault media type means copying ~600 lines for ~90 lines of difference | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P64 | open | 2026-08-31 | The integration suite's login setup fails after a successful sign-in, and `diagnose()` hides why | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P65 | open | 2026-08-31 | Perf tooling measures query count only, so a 12-second render passes every scaling test | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P66 | open | 2026-08-31 | Organize's active label tab still renders its full card list unpaginated | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P67 | open | 2026-08-31 | "Organize this property" fans out ~6-7 queries per candidate pin, uncapped to 500 | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P68 | open | 2026-08-31 | N+1s in the site-admin user list, the achievement icon picker and Memories > Maps still have no perf test | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P69 | open | 2026-08-31 | Unbounded lists with no pagination across most of the site, from album pickers to Immich imports | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P71 | open | 2026-09-04 | The Sphinx setup builds successfully and produces no API documentation at all | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P73 | open | 2026-09-05 | `bun-types` is pinned at 1.1.6 against Bun 1.3.14, so 81 valid assertions look like type errors | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| T1 | done | 2026-08-27 | The mobile team's 2026-07-27 ask list is spent: its P0 was already false and its P2 remainder moved on | [`docs/notes/mobile_app_requirements.md`](notes/mobile_app_requirements.md) |
| T2 | open | 2026-08-27 | HIGH 0-ref findings are all triaged; the MEDIUM tier and Jess's caching requests are still open | [`docs/reports/code_audit_status.txt`](reports/code_audit_status.txt) |
| PL1 | live | 2026-09-01 | The strategy plus a six-tier backlog: locations are both the product and the thing being protected | [`docs/ROADMAP.md`](ROADMAP.md) |
| PL2 | live | 2026-08-27 | Flutter + flutter_map, offline outbox sync and OAuth2+PKCE are the companion app's stack; two risks remain | [`docs/designs/drafts/mobile-app-stack-r2.md`](designs/drafts/mobile-app-stack-r2.md) |
| PL3 | live | 2026-08-27 | Three data classes need three trust models: messages stay strict E2EE, a handed-out vault key covers the rest | [`docs/designs/e2ee-passkey-unlock.md`](designs/e2ee-passkey-unlock.md) |
| PL4 | live | 2026-08-27 | Existence, not detail, is the oracle - so the gate conceals a wiki's contributions rather than degrading it | [`docs/designs/reputation-and-gating.md`](designs/reputation-and-gating.md) |
| PL5 | live | 2026-08-27 | One row per (target, field, write) resolves a per-viewer view in one DISTINCT ON query, with no replay | [`docs/designs/versioned-content.md`](designs/versioned-content.md) |
| PL6 | live | 2026-08-29 | Every test file is being reviewed for negative coverage; 73 of 832 done, resume at manifest line 94 | [`docs/notes/test-quality-audit.md`](notes/test-quality-audit.md) |
| D1 | accepted | 2026-08-27 | Product intent is human-owned: privacy by construction, wiki access must be earned, E2EE is not optional | [`docs/GOALS.md`](GOALS.md) |
| D2 | accepted | 2026-09-01 | Concealment must make a wiki byte-equivalent to a zero-contribution place, so most of the work is aggregates | [`docs/designs/concealed-wiki-spec.md`](designs/concealed-wiki-spec.md) |
| D3 | accepted | 2026-08-27 | One public location per 15km region, gated on five eligibility rules and a community vote - built 2026-07-23 | [`docs/designs/drafts/public-pins-by-vote.md`](designs/drafts/public-pins-by-vote.md) |
| D4 | accepted | 2026-08-27 | Place, with parent_relation and per-domain symmetric access, is the single answer to 'is this the same place?' | [`docs/designs/place-consolidation.md`](designs/place-consolidation.md) |
| D5 | accepted | 2026-08-27 | Thirteen decisions answer the mobile team's asks: what shipped, what was declined, what is deferred | [`docs/notes/mobile_app_notes.md`](notes/mobile_app_notes.md) |
| X1 | holds | 2026-08-27 | A release merge silently dropped --skip-undecryptable from DATA_ENCRYPTION.md; nothing else had drifted | [`docs/audits/DATA_ENCRYPTION_AUDIT.md`](audits/DATA_ENCRYPTION_AUDIT.md) |
| X2 | holds | 2026-09-01 | A gate enforced in the web UI is repeatedly missing from the parallel external-API endpoint | [`docs/audits/FEATURES_CODE_AUDIT.md`](audits/FEATURES_CODE_AUDIT.md) |
| X3 | holds | 2026-08-27 | GOALS.md contradicts the other docs on six points and states ten goals no other doc records at all | [`docs/audits/GOALS_AUDIT.md`](audits/GOALS_AUDIT.md) |
| X4 | holds | 2026-08-27 | Nineteen GOALS.md topics read against code: leaks fixed, but a shared photo is still one row, not a copy | [`docs/audits/GOALS_CODE_AUDIT.md`](audits/GOALS_CODE_AUDIT.md) |
| X5 | holds | 2026-08-27 | LOCATION_DATA_TESTS.md matched its specs except parcel-area bounds a live measurement had already moved | [`docs/audits/LOCATION_DATA_AUDIT.md`](audits/LOCATION_DATA_AUDIT.md) |
| X6 | holds | 2026-08-27 | The contract/integration docs matched their suites; the production-write guard worked but nothing tested it | [`docs/audits/TEST_INFRA_DOCS_AUDIT.md`](audits/TEST_INFRA_DOCS_AUDIT.md) |
| X7 | holds | 2026-08-27 | A 35-unit full-codebase sweep found the bugs cluster in six recurring shapes, not spread across the tree | [`docs/audits/codebase-audit.md`](audits/codebase-audit.md) |
| X8 | holds | 2026-08-27 | Six of fourteen concealment tests did not exercise the code they named, which is why self-review said clean | [`docs/designs/concealment-review-2-2026-08-24.md`](designs/concealment-review-2-2026-08-24.md) |
| X9 | holds | 2026-08-27 | Concealment's substitution functions had no production callers, so flipping the flag would have leaked | [`docs/designs/concealment-review-2026-08-24.md`](designs/concealment-review-2026-08-24.md) |
| X10 | holds | 2026-08-27 | r2 is wrong that push dispatcher wiring remains - v0.6.0 already dispatches native push on every notification | [`docs/designs/drafts/mobile-app-stack-r2-review.md`](designs/drafts/mobile-app-stack-r2-review.md) |
| X11 | holds | 2026-08-27 | The three designed REData follow-ups all shipped; the durable gap list is a route diff in PROBLEMS.md | [`docs/designs/redata-integration.md`](designs/redata-integration.md) |
| X12 | holds | 2026-08-27 | 100 write handlers, 1,217 statements of data-mutating view code, never execute under any test | [`docs/reports/2026-08-14-view-coverage.md`](reports/2026-08-14-view-coverage.md) |
| X13 | holds | 2026-07-30 | The self-hosted Overpass instance beats every mirror; three pool members were dead or Swiss-only | [`docs/reports/overpass-mirror-test.md`](reports/overpass-mirror-test.md) |
| I1 | unvalidated | 2026-08-27 | Splitting into a near-zero-knowledge server and a data-holding agent was planned in full, then deferred | [`docs/designs/rejected-and-deferred/split-architecture.md`](designs/rejected-and-deferred/split-architecture.md) |
| I2 | actionable | 2026-08-27 | Ten free/open APIs surveyed as integration candidates; several have since shipped as plugins, so re-check before using it | [`docs/reports/api-expansion-candidates.md`](reports/api-expansion-candidates.md) |
| I3 | absorbed | 2026-07-30 | SpotGuessr's backend was sound and its frontend was the debt; all five recommendations shipped | [`docs/reports/spotguessr-audit.md`](reports/spotguessr-audit.md) |
| R1 | current | 2026-09-02 | The assistant reaches a provider only through three credential-narrowed tiers behind a default-deny egress proxy | [`docs/AI_PIPELINE.md`](AI_PIPELINE.md) |
| R2 | current | 2026-08-27 | Schemathesis holds the external API to its own published OpenAPI doc; detail routes still only prove 404 handling | [`docs/CONTRACT_TESTS.md`](CONTRACT_TESTS.md) |
| R3 | stale | 2026-08-31 | Field encryption covers identity/credential/contact data only; core location content is left to disk encryption | [`docs/DATA_ENCRYPTION.md`](DATA_ENCRYPTION.md) |
| R4 | current | 2026-08-27 | Demo isolation is a separate deployment, not a realm column, because ~20 visibility guards would fail open | [`docs/DEMO.md`](DEMO.md) |
| R5 | current | 2026-09-04 | The v1 external API surface: two bearer credential kinds, per-scope gating everywhere, additive-only versioning | [`docs/EXTERNAL_API.md`](EXTERNAL_API.md) |
| R6 | current | 2026-09-02 | Every shipped UrbanLens feature is inventoried here, so a "new feature" request is usually already built | [`docs/FEATURES.md`](FEATURES.md) |
| R7 | current | 2026-09-01 | A Playwright suite driving a deployed instance catches what a single-process pytest run structurally cannot | [`docs/INTEGRATION_TESTS.md`](INTEGRATION_TESTS.md) |
| R8 | current | 2026-08-27 | A plausible boundary is not a sourced one, so the HRSH specs assert provenance and bounds, never values | [`docs/LOCATION_DATA_TESTS.md`](LOCATION_DATA_TESTS.md) |
| R9 | current | 2026-09-02 | Uploads decode only in a network-isolated worker; served bytes are re-encoded, never the ones uploaded | [`docs/MEDIA_PIPELINE.md`](MEDIA_PIPELINE.md) |
| R10 | current | 2026-09-03 | /metrics is off by default and unrouted when off, and undercounts silently unless multiprocess mode is on | [`docs/METRICS.md`](METRICS.md) |
| R11 | current | 2026-09-03 | Twenty-nine non-obvious behaviours that read as bugs until explained; eleven source files cite it by name | [`docs/NOTES.md`](NOTES.md) |
| R12 | current | 2026-08-27 | Nothing is visible until both the container gate and the owner's settings gate say yes; three items still open | [`docs/PRIVACY_MODEL.md`](PRIVACY_MODEL.md) |
| R13 | current | 2026-09-04 | Every diagnostic here exists because a specific defect got through without it; twelve checkers now run in CI | [`docs/TOOLING.md`](TOOLING.md) |
| R14 | current | 2026-09-01 | Where a fallback exists, asserting the shape of the answer passes forever - assert provenance instead | [`docs/audits/TEST_COVERAGE_GAPS.md`](audits/TEST_COVERAGE_GAPS.md) |
| R15 | current | 2026-09-01 | SpotGuessr's rules: pinned-by-everyone eligibility, Glicko-2 for players and locations, wiki-only photos | [`docs/designs/drafts/spotguessr.md`](designs/drafts/spotguessr.md) |
| R16 | current | 2026-08-27 | Trivia reuses SpotGuessr eligibility and Glicko-2, funnelling all three question sources through one classifier | [`docs/designs/drafts/trivia.md`](designs/drafts/trivia.md) |
| R17 | current | 2026-08-27 | DM text is E2EE against a stolen database, not a malicious operator; forward secrecy is traded for recovery | [`docs/designs/e2ee.md`](designs/e2ee.md) |
| R18 | current | 2026-09-01 | Onboarding is dismissible in-context coach cards on five surfaces, not a blocking tour | [`docs/designs/onboarding_plan.md`](designs/onboarding_plan.md) |
| R19 | current | 2026-09-01 | One UrbanLensPlugin subclass bundles an integration's rate limits, panels, providers and hook callbacks | [`docs/designs/plugins.md`](designs/plugins.md) |
| R20 | current | 2026-07-30 | Decoding a Google CID's S2 hex is wrong by >500m for 31.3% of places, so CIDs must be resolved via REData | [`docs/designs/redata-cid-resolution.md`](designs/redata-cid-resolution.md) |
| R21 | current | 2026-08-27 | SpotGuessr's mobile API is solo-only, and answer fields are whitelisted out of an unrevealed round | [`docs/mobile/api-documentation.md`](mobile/api-documentation.md) |
| R22 | current | 2026-08-29 | The frozen 832-file manifest the test-quality audit addresses batches by line number | [`docs/notes/test-quality-audit-files.txt`](notes/test-quality-audit-files.txt) |
| R23 | current | 2026-07-22 | The re-runnable harness that produced the 120 Overpass measurements across six endpoints | [`docs/reports/overpass_bench.py`](reports/overpass_bench.py) |
| R24 | current | 2026-07-22 | The 120 raw Overpass rows the mirror verdicts were computed from | [`docs/reports/overpass_mirror_results.json`](reports/overpass_mirror_results.json) |
| R25 | stale | 2026-08-27 | A generated 0-reference scan whose line numbers no longer resolve; regenerate instead of reading | [`docs/reports/unused_functions.txt`](reports/unused_functions.txt) |
| N1 | stale | 2026-09-03 | The Celery requeue loop was a two-request DoS; fixed, and the durable version now lives in NOTES.md | [`docs/NOTES-celery-acks.md`](NOTES-celery-acks.md) |
| N2 | current | 2026-08-27 | 82 ways a gated wiki gives itself away collapse to eleven classes and three viewer-less chokepoints | [`docs/designs/reputation-gating-tells.md`](designs/reputation-gating-tells.md) |
| N3 | stale | 2026-08-27 | A 631-chunk audit log whose fixes landed and whose open items were refiled into docs/PROBLEMS.md | [`docs/reports/2026-08-11-codebase-audit.md`](reports/2026-08-11-codebase-audit.md) |
| N4 | current | 2026-07-30 | Round-2 staging UAT: its two criticals (email exposure, raw-coordinate pin names) are now fixed | [`docs/reports/claude_uat.md`](reports/claude_uat.md) |
| N5 | current | 2026-07-30 | Round-3 UAT found messaging and settings working; both 'ongoing' criticals have since been fixed | [`docs/reports/claude_uat_r3.md`](reports/claude_uat_r3.md) |
| N6 | current | 2026-07-22 | Round-1 staging UAT: 4 criticals, from a misconfigured staging API key to email on public profiles | [`docs/reports/ua_testing.md`](reports/ua_testing.md) |
