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

**Next free id:** `P113` · `T3` · `PL8` · `D13` · `X18` · `I5` · `R29` · `N21`

Ids are allocated here and never reused or renumbered. Add the row in the same
commit as the entry, so a duplicate id becomes a merge conflict rather than a
silent collision. `docs/README.md` has the record types, the status values for
each, and the house style. `P` numbers entries inside `PROBLEMS.md`; every other
prefix numbers a whole document.

Resolved problems are **not** listed here — they move to
[`archive/PROBLEMS-ARCHIVE.md`](archive/PROBLEMS-ARCHIVE.md), which holds 314
of them. This index is what is live. Grep the archive before concluding a defect
is new - and an archived entry keeps its `id:` line, so a citation of `P70`
still resolves after it is fixed, and the id is never handed out again.

| id | status | updated | claim | path |
|---|---|---|---|---|
| P1 | open | 2026-09-01 | VirusTotal scanning is hash-lookup-only, so a file VirusTotal has never seen falls back to ClamAV forever | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P2 | open | 2026-08-31 | `parse_for_preview` parses archives and KML in the request, blocking `UL_UNTRUSTED_PARSE_POLICY=deny` | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P3 | open | 2026-08-31 | The pin-detail hero no longer links to `PinRelinkView.get`, orphaning the `pin.link` wiki picker | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P5 | open | 2026-08-25 | Dialog forms post every field and handlers save every column, so untouched values overwrite and re-attribute | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P6 | open | 2026-08-21 | Production REData still 404s `/api/v1/public-locations/`, so a fresh dev environment seeds no catalog pins | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P7 | open | 2026-08-19 | nginx pins its app upstream at config load and REData's `ref` is stored as permanent identity | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P9 | open | 2026-09-08 | REData gaps: mostly closed 2026-09-08; `?limit=` is REData-side, land-use-area geometry needs a map-overlay decision | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P11 | open | 2026-09-06 | 84 raw `fetch()` calls bypass `fetch-json.ts`, and "all the wrappers are gone" was a count, not a search | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P13 | open | 2026-07-23 | Pin-detail external-data freshness is one site-wide `external_data_cache_days` knob, not per-source | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P14 | open | 2026-09-05 | Custom pin and label icons are readable by any authenticated user; narrowing that needs a pin-visibility query nothing has | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P15 | open | 2026-07-22 | openresty's 90s proxy cap cuts any Overpass query needing longer, whatever `[timeout:N]` asked for | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P16 | open | 2026-07-22 | Aliases and label membership are still strictly per-pin, with no aggregation across child pins | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P19 | open | 2026-09-06 | Audit re-verification's residual gaps remain: a 1,100-line `_dark.scss`, a stub AI gateway, blocking AI in the request | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P20 | open | 2026-07-25 | The legacy-CID repair leaves the CID on the wrong `Location`, so `by_cid()` resolves it wrongly for everyone | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P21 | open | 2026-09-05 | A shared markup map stamps provenance only for places its sender has pinned | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P22 | open | 2026-07-31 | REData's `/api/v1/parcels/lookup/` crash-loops gunicorn workers with OOM/WORKER TIMEOUT on chiron | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P23 | open | 2026-07-31 | The production celery worker's env sets `UL_SITE_URL=staging.urbanlens.org`, so built URLs point at staging | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P24 | open | 2026-08-05 | A campus pin aggregates only the nearest CRIS building's media, not the survey's full USN roster | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P25 | open | 2026-08-07 | `Comment.profile` CASCADEs but `TripComment.author` SET_NULLs, so account deletion erases only some comments | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P26 | open | 2026-09-06 | A group message can still be sent under a stale key version, and refusing one risks an availability outage | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P27 | open | 2026-08-08 | Saved-filter regions use leaflet-draw's transactional remove tool, so deleted polygons resurrect on the next draw | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P86 | open | 2026-09-07 | Deleting a contribution outright leaves its reputation points standing; the fix is a weight, not a retraction | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P29 | open | 2026-08-13 | 186 write routes have no test naming them; the smoke sweep proves only that they do not 5xx | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P34 | open | 2026-08-13 | 22,636 lines of inline template JS sit outside every automated check, with duplicated escaping helpers | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P35 | open | 2026-09-05 | Two named routes have no production caller; the other five the sweep flagged are reached by hardcoded path | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P36 | open | 2026-09-05 | 50 BEM modifiers are applied in templates with no CSS rule, so intended visual states never render | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P37 | open | 2026-09-08 | 100 write handlers totalling 1,217 statements never execute under the test suite | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P41 | open | 2026-09-06 | The queryset API's unused half, by call graph: 26 methods deleted, 27 test-only ones left | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P47 | open | 2026-08-16 | A deleted message's preview survives in the recipient's notification list | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P49 | open | 2026-09-05 | Doc citations drift silently, and a pin-suggestion race can still duplicate a row | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P50 | open | 2026-09-05 | `test_safety_chat` and `test_migration_0039_reverse` fail only under a randomized suite order | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P51 | open | 2026-08-22 | Native `<select>` popups stay light-on-light in dark mode despite `color-scheme: dark` | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P53 | open | 2026-09-06 | The Private Pin page's opening burst is bounded now, but its tail is 15 seconds longer | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P55 | open | 2026-09-06 | A withdrawn contribution keeps its reputation event, and the wiki gallery's delete strings are false there | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P56 | open | 2026-09-05 | `Cross-Origin-Embedder-Policy` is report-only pending one measurement; `require-corp` is ruled out | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P57 | open | 2026-09-06 | The test-quality audit's follow-ups: 13 done; three untested surfaces, two unproven locks and two decisions remain | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P58 | open | 2026-09-06 | A renamed photo's old URL still 404s for the uploader who just uploaded it | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P59 | open | 2026-09-06 | A `lightbox-associations.webp` thumbnail on the `ae97b86` dev account is durably broken, not just racing | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P63 | open | 2026-08-31 | Adding a third Vault media type means copying ~600 lines for ~90 lines of difference | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P66 | open | 2026-08-31 | Organize's active label tab still renders its full card list unpaginated | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P69 | open | 2026-09-08 | Unbounded lists across the site: 9 of 11 fixed; one argued against by measurement, one group deliberately left | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| T1 | done | 2026-08-27 | The mobile team's 2026-07-27 ask list is spent: its P0 was already false and its P2 remainder moved on | [`docs/notes/mobile_app_requirements.md`](notes/mobile_app_requirements.md) |
| T2 | open | 2026-08-27 | HIGH 0-ref findings are all triaged; the MEDIUM tier and Jess's caching requests are still open | [`docs/reports/code_audit_status.txt`](reports/code_audit_status.txt) |
| PL1 | live | 2026-09-01 | The strategy plus a six-tier backlog: locations are both the product and the thing being protected | [`docs/ROADMAP.md`](ROADMAP.md) |
| PL2 | live | 2026-08-27 | Flutter + flutter_map, offline outbox sync and OAuth2+PKCE are the companion app's stack; two risks remain | [`docs/designs/drafts/mobile-app-stack-r2.md`](designs/drafts/mobile-app-stack-r2.md) |
| PL3 | live | 2026-08-27 | Three data classes need three trust models: messages stay strict E2EE, a handed-out vault key covers the rest | [`docs/designs/e2ee-passkey-unlock.md`](designs/e2ee-passkey-unlock.md) |
| PL4 | live | 2026-08-27 | Existence, not detail, is the oracle - so the gate conceals a wiki's contributions rather than degrading it | [`docs/designs/reputation-and-gating.md`](designs/reputation-and-gating.md) |
| PL5 | live | 2026-08-27 | One row per (target, field, write) resolves a per-viewer view in one DISTINCT ON query, with no replay | [`docs/designs/versioned-content.md`](designs/versioned-content.md) |
| PL6 | live | 2026-08-29 | Every test file is being reviewed for negative coverage; 73 of 832 done, resume at manifest line 94 | [`docs/notes/test-quality-audit.md`](notes/test-quality-audit.md) |
| PL7 | live | 2026-09-10 | Making "no user can affect another user's availability" a property the tests can prove; phase 0 done, 1 underway | [`docs/notes/availability-isolation-programme.md`](notes/availability-isolation-programme.md) |
| P82 | open | 2026-09-06 | At exactly 768px the nav needs 837px, so a tablet-width viewport still scrolls sideways | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P83 | open | 2026-09-06 | Over half of every page's HTML is inline `<script>`, re-sent uncached on every load | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P85 | open | 2026-09-06 | Every manager is a dynamic base class, so `Model.objects` is `Any` and 146 mypy errors are turned off to hide it | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P89 | open | 2026-09-08 | `MarkupJsonView`'s `?children=1` wiki path skips concealment; dormant only because `concealment_active()` is hardcoded False | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P90 | open | 2026-09-08 | `backfill_wiki_edit_points`, extracted from its migration specifically to be testable, has no test | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P91 | open | 2026-09-08 | Seven of eight new security integration specs have never run against a live deployment | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P92 | open | 2026-09-08 | `map-clusters.ts`'s cluster badge constants are duplicated, not shared, by the main map's inline script | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P93 | open | 2026-09-08 | Nine REData plugins declare no rate-limit defaults for their own gateway's service key | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P95 | open | 2026-09-10 | `ExtractionBudget` cannot bound a single file's decompression, and nothing prices what parsing one costs | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P96 | open | 2026-09-10 | `import_confirmed`'s SSE import creates as many Pins as the client claims, synchronously in the web worker | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P97 | open | 2026-09-10 | `dissolve_polygons` is O(n^3) GEOS work over an uncapped user-supplied polygon count | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P98 | open | 2026-09-10 | The site-admin system panel re-walks the whole media tree on every load, gated only by admin permission | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P100 | open | 2026-09-10 | Map search-box autocomplete runs 8 leading-wildcard `ILIKE`s with zero trigram indexes to serve them | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P103 | open | 2026-09-10 | `MEDIA_PIPELINE.md`'s "every parser is now guarded" was false; a label-icon resize decodes unsandboxed in-request | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P104 | open | 2026-09-10 | Celery can starve the web tier by exhausting Postgres connections, not CPU; this already caused an 11-hour outage | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P105 | open | 2026-09-10 | A Valkey outage 500s every request after 32 seconds, including the readiness probe | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P107 | open | 2026-09-10 | The saved-filter count badges read every pin in the account to draw a number | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P109 | open | 2026-09-10 | One import's task fan-out fills the only Celery queue for hours, and a safety task waits behind it | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P110 | open | 2026-09-10 | The Overture OOM fix is best-effort, and Overture rate-limiting us is what turns it off | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P111 | open | 2026-09-10 | A gunicorn worker's memory is set by peak concurrent response size, and it never gives it back | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| P112 | open | 2026-09-10 | `bun run codeql:gate` fails with 26 untriaged findings, so nobody runs it | [`docs/PROBLEMS.md`](PROBLEMS.md) |
| D1 | accepted | 2026-08-27 | Product intent is human-owned: privacy by construction, wiki access must be earned, E2EE is not optional | [`docs/GOALS.md`](GOALS.md) |
| D2 | accepted | 2026-09-01 | Concealment must make a wiki byte-equivalent to a zero-contribution place, so most of the work is aggregates | [`docs/designs/concealed-wiki-spec.md`](designs/concealed-wiki-spec.md) |
| D3 | accepted | 2026-08-27 | One public location per 15km region, gated on five eligibility rules and a community vote - built 2026-07-23 | [`docs/designs/drafts/public-pins-by-vote.md`](designs/drafts/public-pins-by-vote.md) |
| D4 | accepted | 2026-08-27 | Place, with parent_relation and per-domain symmetric access, is the single answer to 'is this the same place?' | [`docs/designs/place-consolidation.md`](designs/place-consolidation.md) |
| D5 | accepted | 2026-08-27 | Thirteen decisions answer the mobile team's asks: what shipped, what was declined, what is deferred | [`docs/notes/mobile_app_notes.md`](notes/mobile_app_notes.md) |
| D6 | accepted | 2026-09-06 | Media may live in an object store, but a read is never a presigned URL - the gate stays in the data path | [`docs/designs/media-object-storage.md`](designs/media-object-storage.md) |
| D7 | accepted | 2026-09-06 | Over-cap uploads become chunked posts to Django, not presigned multipart; until then the app advertises the ingress cap | [`docs/designs/large-upload-protocol.md`](designs/large-upload-protocol.md) |
| D8 | accepted | 2026-09-06 | Storage quotas are enforced generally, not exactly: an over-quota profile keeps its assets and is barred from uploading more | [`docs/designs/storage-running-total.md`](designs/storage-running-total.md) |
| D9 | accepted | 2026-09-07 | A moderator's removal costs reputation slightly and reversibly - a per-event weight, not the binary retraction | [`docs/designs/reputation-removal-weighting.md`](designs/reputation-removal-weighting.md) |
| D10 | accepted | 2026-09-08 | A block's incident history is its own paid flag, not `NEARBY_RESEARCH` - it is a distinct pricing lever, not a variant of one | [`docs/designs/incident-history-feature-gate.md`](designs/incident-history-feature-gate.md) |
| D11 | accepted | 2026-09-10 | One user's expensive request must be unable to reach another user's request, and the way to guarantee that is bounded pools with named budgets | [`docs/designs/request-isolation-and-connection-budget.md`](designs/request-isolation-and-connection-budget.md) |
| D12 | accepted | 2026-09-11 | The map cache becomes an accelerator the site can lose, and labels stop being copied into every pin; built, with the delta and viewport mode deferred | [`docs/designs/map-data-contract-v11.md`](designs/map-data-contract-v11.md) |
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
| X14 | holds | 2026-09-10 | Switching the render mixin to a CPU clock separates the classes worse, not better; keep `perf_counter` | [`docs/notes/render-clock-calibration.md`](notes/render-clock-calibration.md) |
| X15 | holds | 2026-09-10 | The first neighbour run, and the correction: on the real process model one user filtering costs another user nothing | [`docs/notes/first-neighbour-run.md`](notes/first-neighbour-run.md) |
| X16 | holds | 2026-09-10 | The four chaos scenarios, run for the first time: one catastrophic, three clean | [`docs/notes/first-chaos-run.md`](notes/first-chaos-run.md) |
| X17 | holds | 2026-09-11 | What one account's map data actually costs: the cache is 662 bytes a pin, not 1,700 | [`docs/notes/map-data-cost-measured.md`](notes/map-data-cost-measured.md) |
| I1 | unvalidated | 2026-08-27 | Splitting into a near-zero-knowledge server and a data-holding agent was planned in full, then deferred | [`docs/designs/rejected-and-deferred/split-architecture.md`](designs/rejected-and-deferred/split-architecture.md) |
| I2 | actionable | 2026-08-27 | Ten free/open APIs surveyed as integration candidates; several have since shipped as plugins, so re-check before using it | [`docs/reports/api-expansion-candidates.md`](reports/api-expansion-candidates.md) |
| I3 | absorbed | 2026-07-30 | SpotGuessr's backend was sound and its frontend was the debt; all five recommendations shipped | [`docs/reports/spotguessr-audit.md`](reports/spotguessr-audit.md) |
| I4 | absorbed | 2026-09-06 | Exact storage quotas need the total on one lockable row; the hard part is the five places `file_size` changes - declined, see D8 | [`docs/designs/storage-running-total.md`](designs/storage-running-total.md) |
| R1 | current | 2026-09-02 | The assistant reaches a provider only through three credential-narrowed tiers behind a default-deny egress proxy | [`docs/AI_PIPELINE.md`](AI_PIPELINE.md) |
| R2 | current | 2026-08-27 | Schemathesis holds the external API to its own published OpenAPI doc; detail routes still only prove 404 handling | [`docs/CONTRACT_TESTS.md`](CONTRACT_TESTS.md) |
| R3 | stale | 2026-08-31 | Field encryption covers identity/credential/contact data only; core location content is left to disk encryption | [`docs/DATA_ENCRYPTION.md`](DATA_ENCRYPTION.md) |
| R4 | current | 2026-08-27 | Demo isolation is a separate deployment, not a realm column, because ~20 visibility guards would fail open | [`docs/DEMO.md`](DEMO.md) |
| R5 | current | 2026-09-04 | The v1 external API surface: two bearer credential kinds, per-scope gating everywhere, additive-only versioning | [`docs/EXTERNAL_API.md`](EXTERNAL_API.md) |
| R6 | current | 2026-09-08 | Every shipped UrbanLens feature is inventoried here, so a "new feature" request is usually already built | [`docs/FEATURES.md`](FEATURES.md) |
| R7 | current | 2026-09-01 | A Playwright suite driving a deployed instance catches what a single-process pytest run structurally cannot | [`docs/INTEGRATION_TESTS.md`](INTEGRATION_TESTS.md) |
| R8 | current | 2026-08-27 | A plausible boundary is not a sourced one, so the HRSH specs assert provenance and bounds, never values | [`docs/LOCATION_DATA_TESTS.md`](LOCATION_DATA_TESTS.md) |
| R9 | current | 2026-09-10 | Uploads decode only in a network-isolated worker, except one unsandboxed label-icon resize (P103) | [`docs/MEDIA_PIPELINE.md`](MEDIA_PIPELINE.md) |
| R10 | current | 2026-09-03 | /metrics is off by default and unrouted when off, and undercounts silently unless multiprocess mode is on | [`docs/METRICS.md`](METRICS.md) |
| R11 | current | 2026-09-03 | Twenty-nine non-obvious behaviours that read as bugs until explained; eleven source files cite it by name | [`docs/NOTES.md`](NOTES.md) |
| R12 | current | 2026-08-27 | Nothing is visible until both the container gate and the owner's settings gate say yes; three items still open | [`docs/PRIVACY_MODEL.md`](PRIVACY_MODEL.md) |
| R13 | current | 2026-09-10 | Every diagnostic here exists because a specific defect got through without it; twelve checkers now run in CI | [`docs/TOOLING.md`](TOOLING.md) |
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
| R26 | current | 2026-09-05 | Restoring these plain-SQL dumps needs an empty target and psql from the app container; round trip verified | [`docs/BACKUPS.md`](BACKUPS.md) |
| R27 | current | 2026-09-10 | The map payload's cost was 88% Python object construction, not SQL; the fix is query- and allocation-flat | [`docs/MAP_PERFORMANCE.md`](MAP_PERFORMANCE.md) |
| R28 | current | 2026-09-10 | The WSGI tier runs gevent with no recorded rationale, contradicting reasoning the project applied everywhere else it chose a worker model | [`docs/notes/wsgi-worker-model-and-connections.md`](notes/wsgi-worker-model-and-connections.md) |
| N1 | stale | 2026-09-03 | The Celery requeue loop was a two-request DoS; fixed, and the durable version now lives in NOTES.md | [`docs/archive/NOTES-celery-acks.md`](archive/NOTES-celery-acks.md) |
| N2 | current | 2026-08-27 | 82 ways a gated wiki gives itself away collapse to eleven classes and three viewer-less chokepoints | [`docs/designs/reputation-gating-tells.md`](designs/reputation-gating-tells.md) |
| N3 | stale | 2026-08-27 | A 631-chunk audit log whose fixes landed and whose open items were refiled into docs/PROBLEMS.md | [`docs/reports/2026-08-11-codebase-audit.md`](reports/2026-08-11-codebase-audit.md) |
| N4 | current | 2026-07-30 | Round-2 staging UAT: its two criticals (email exposure, raw-coordinate pin names) are now fixed | [`docs/reports/claude_uat.md`](reports/claude_uat.md) |
| N5 | current | 2026-07-30 | Round-3 UAT found messaging and settings working; both 'ongoing' criticals have since been fixed | [`docs/reports/claude_uat_r3.md`](reports/claude_uat_r3.md) |
| N6 | current | 2026-07-22 | Round-1 staging UAT: 4 criticals, from a misconfigured staging API key to email on public profiles | [`docs/reports/ua_testing.md`](reports/ua_testing.md) |
| N7 | current | 2026-09-06 | Two Django template traps that each shipped a 500: a filter argument has no failure tolerance, and `.image.url` raises | [`docs/notes/template-render-traps.md`](notes/template-render-traps.md) |
| N8 | current | 2026-09-06 | Reply to the infrastructure repo's two open handoffs: /static/ 404s, media on Garage, and the 100 MB body cap | [`docs/handoffs/infrastructure-media-and-static.md`](handoffs/infrastructure-media-and-static.md) |
| N9 | current | 2026-09-08 | Reply on migration 0054: its IntegrityError confirmed and fixed, and why a stub-driven test could not have caught it | [`docs/handoffs/infrastructure-0054-friendship-merge.md`](handoffs/infrastructure-0054-friendship-merge.md) |
| N10 | current | 2026-09-10 | A seed-then-measure benchmark without ANALYZE measures the planner's ignorance, not the query - cost this investigation 3 rounds | [`docs/notes/map-perf-measurement-and-test-gaps.md`](notes/map-perf-measurement-and-test-gaps.md) |
| N11 | current | 2026-09-10 | Query- and render-time scaling mixins are structurally blind to a per-row object-count regression; a third axis now exists | [`docs/notes/map-perf-measurement-and-test-gaps.md`](notes/map-perf-measurement-and-test-gaps.md) |
| N12 | current | 2026-09-10 | `pyproject.toml`'s nplusone-rejection comment misdescribed `django-auto-prefetch`; it is wholly unwired | [`docs/notes/map-perf-doc-corrections.md`](notes/map-perf-doc-corrections.md) |
| N13 | current | 2026-09-10 | The archived "map payload is already query-flat" claim was true and answered the wrong question | [`docs/notes/map-perf-doc-corrections.md`](notes/map-perf-doc-corrections.md) |
| N14 | current | 2026-09-10 | Nothing in pytest or local dev exercises the shared-connection-pool topology that caused P104's outage | [`docs/notes/wsgi-worker-model-and-connections.md`](notes/wsgi-worker-model-and-connections.md) |
| N15 | current | 2026-09-10 | `celery-metrics` has been crash-looping on staging since it was deployed, because nothing gates it on the flag it requires | [`docs/notes/celery-metrics-crash-loop.md`](notes/celery-metrics-crash-loop.md) |
| N16 | current | 2026-09-10 | Asks infrastructure for a dev environment that runs gunicorn, and where availability chaos belongs given drills are explicitly not a test suite | [`docs/handoffs/infrastructure-availability-drills-and-gunicorn-dev-envs.md`](handoffs/infrastructure-availability-drills-and-gunicorn-dev-envs.md) |
| N17 | current | 2026-09-10 | Reply to infrastructure: both their corrections hold, their broker finding invalidates part of our chaos table, and N15's restart count is confirmable | [`docs/handoffs/infrastructure-availability-drills-reply.md`](handoffs/infrastructure-availability-drills-reply.md) |
| N18 | current | 2026-09-10 | Follow-up to infrastructure: the metrics override guards a branch that does not exist, and their Docker-access plan item is already satisfied | [`docs/handoffs/infrastructure-availability-drills-followup.md`](handoffs/infrastructure-availability-drills-followup.md) |
| N19 | current | 2026-09-10 | The staging metrics exporter's 30-hour restart loop is stopped at 4,410; the permanent fix is a 52-commit staging deploy, not a container fix | [`docs/handoffs/infrastructure-metrics-exporter-loop-closed.md`](handoffs/infrastructure-metrics-exporter-loop-closed.md) |
| N20 | current | 2026-09-10 | The neighbour test runs, and what it measured changes what staging should expect | [`docs/handoffs/infrastructure-neighbour-test-results.md`](handoffs/infrastructure-neighbour-test-results.md) |
