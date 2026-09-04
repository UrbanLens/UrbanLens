# Test-infrastructure docs vs. code: audit

Generated 2026-08-25, by reading the actual test-infrastructure implementation for concrete
claims in `docs/CONTRACT_TESTS.md` (the schemathesis suite, `tests/contract/`) and
`docs/INTEGRATION_TESTS.md` (the on-demand Playwright suite, `tests/integration/`). A different
surface again from `docs/audits/GOALS_CODE_AUDIT.md` and `docs/audits/FEATURES_CODE_AUDIT.md`: this round checks
whether the docs describing *how the test suites themselves work* still match the actual test
code - not app behavior. One topic (the integration suite's production-write guard) was
deliberately chosen as the highest-stakes claim in either doc: a wrong claim there could leave
someone believing they're protected from running destructive writes against a real production
deployment when they aren't.

**How to read status:** `MATCHES` = the code does exactly what the doc says, today. `PARTIAL` =
mostly true with a real caveat. `STALE` = the doc was accurate once but the code has since
changed (a documentation-currency issue, not a live bug). `CONTRADICTS` = the code does something
different from - or the opposite of - what the doc claims, today.

`STALE`/`CONTRADICTS` findings were independently adversarially verified (a second agent tried to
refute each by re-reading the cited code itself).

## Summary

| Topic | Status | Headline |
|---|---|---|
| [Contract: operationId/401 findings](#contract-operationid401-findings) | ~~STALE~~ **doc updated** | Both issues were fixed 2026-08-24 (confirmed in code and guard tests); the doc's "What the first run found" section read as an open list with no resolution note - doc corrected |
| [Contract: `ignored_auth` exclusion](#contract-ignored_auth-exclusion) | MATCHES | Genuinely excluded from every check-set variant; Playwright's `api/auth.spec.ts` genuinely covers real auth rejection |
| [Contract: the three traps](#contract-the-three-traps) | MATCHES | All three guards (`_assert_on_a_test_database`, the WSGI-vs-test-client distinction, the connection-keepalive fixture) exist and work as described |
| [Integration: console guard](#integration-console-guard) | MATCHES | Fails on console error/exception/failed subresource exactly as claimed; `guard.allow()`/`strictConsole:false` both work as documented |
| [Integration: HTMX swap timing](#integration-htmx-swap-timing) | MATCHES | `withHtmxSwap` genuinely reads the counter before acting - can't be satisfied by an already-happened swap |
| [Integration: write-quota retry logic](#integration-write-quota-retry-logic) | ~~MATCHES~~ **doc updated** | 429 retry/cap logic matches exactly; one adjacent stale number found and fixed (doc said 90s test timeout, actual default is 180s) |
| [Integration: production guard](#integration-production-guard) | MATCHES, ~~untested~~ **now tested** | Both locks (hostname denylist, provisioning's two-flag check) work exactly as documented and exact-match correctly - but had zero automated coverage on the TypeScript side; added |
| [Integration: known gaps still accurate](#integration-known-gaps-still-accurate) | MATCHES | Both cited gaps (per-worker run id, wiki-creation-endpoint absence) are still real and still accurately recorded |

---

## Contract: operationId/401 findings

**Claim** (`docs/CONTRACT_TESTS.md` "What the first run found"): `passkey_wrap_create`/
`passkey_wrap_destroy` operationIds collide across two routes, and no authenticated operation
documents a 401 - "Both are recorded in `docs/PROBLEMS.md`."

**Verdict: STALE, doc updated.** Both findings were real when written, and both genuinely are
recorded in `docs/PROBLEMS.md` - but that entry has since been updated to
`~~RESOLVED 2026-08-24~~`, and the code confirms the fix is actually in place: the passkey-wrap
view was split into `E2EEPasskeyWrapView` (POST only) and `E2EEPasskeyWrapItemView` (DELETE only)
so no operationId collides, and a new postprocessing hook
(`external_api.schema.document_error_responses`) `setdefault`s 401/403 on every operation
declaring `security`. Both fixes are guarded by tests
(`test_operation_ids_are_unique`, `test_authenticated_operations_document_rejection`,
`tests/contract/test_openapi_conformance.py`). `docs/CONTRACT_TESTS.md`'s "What the first run
found" section, in isolation, still read as an open-findings list with no resolution note - a
reader who didn't follow through to `PROBLEMS.md` would wrongly conclude the schema still has
these two gaps. Independently adversarially verified (confirmed).

**Fixed**: `docs/CONTRACT_TESTS.md` now states both were fixed 2026-08-24, names the actual fix
for each, and reframes the section as the reason the two guard tests exist rather than as open
findings.

## Contract: `ignored_auth` exclusion

**Claim**: `ignored_auth` is permanently excluded from the schemathesis check set (it can only
tamper with a credential it generated, and this suite supplies a real one outside that model);
real auth rejection is covered by `tests/integration/specs/api/auth.spec.ts`.

**Verdict: MATCHES.** `response_checks()` (`tests/contract/schema_source.py`) builds the applied
check list as exactly `[not_a_server_error, response_schema_conformance]`, with
`status_code_conformance`/`content_type_conformance` added only under `UL_CONTRACT_STRICT` -
`ignored_auth` is never added under any mode. `api/auth.spec.ts` exists and asserts real rejection
(401 unauthenticated, 401 malformed key, 401 foreign bearer token, 403 for both a
scope-insufficient read and write) against a live deployment.

No action needed.

## Contract: the three traps

**Claim** (`docs/CONTRACT_TESTS.md` "Three traps, already paid for"): `django_db_blocker.unblock()`
and `request.getfixturevalue("db")` are both dead ends that were tried and rejected; a dedicated
guard (`conftest._assert_on_a_test_database`) catches a third variation; a raw WSGI callable lacks
the test client's `close_old_connections` disconnect, which can silently discard the transaction
holding test fixtures.

**Verdict: MATCHES.** `_assert_on_a_test_database` (`tests/contract/conftest.py`) genuinely exists,
is genuinely called from the `contract_headers` fixture for in-process mode, and raises
`ContractConfigurationError` if the connected DB name doesn't look like a test database. The
WSGI-callable trap is real - the suite does drive Django's real `get_wsgi_application()`, not the
test client - but the suite separately neutralizes the specific symptom the doc warns about via an
autouse `_keep_the_test_connection_open` fixture that disconnects/reconnects
`close_old_connections` around each in-process test. The doc doesn't name that mitigation
explicitly, but its claim ("nothing does that here" about the bare WSGI callable) is accurate as a
description of what the callable itself lacks, which is exactly why the suite layers its own fix
on top.

No action needed.

## Integration: console guard

**Claim**: every UI test fails if its page logged a console error, threw, or failed to fetch a
subresource; narrowed via `guard.allow(regex)`, disabled via `test.use({ strictConsole: false })`.

**Verdict: MATCHES.** `PageGuard` (`tests/integration/lib/page-guard.ts`) listens for console
errors, uncaught exceptions, failed requests, and 4xx/5xx subresource responses; the `page` fixture
(`lib/fixtures.ts`) attaches it before navigation and throws on teardown when `strictConsole`
(default `true`) is on and something unallowed was recorded - but only when the test would
otherwise have passed, so a guard failure never masks a more informative pre-existing failure. All
39 spec files import from the guarded fixtures module. Both documented examples
(`guard.allow(/tools\/export\/status\//)`, `strictConsole: false` on the Swagger UI page) exist
verbatim in the code.

One latent, currently-inert gap noted in passing: the separate `secondaryPage` fixture (a second
browser context) is never wrapped in a `PageGuard`, so a spec that only interacted with
`secondaryPage` wouldn't get this protection for it. No spec currently uses `secondaryPage`, so
this doesn't contradict the doc's claim about "its page" (the primary fixture) - flagged below in
case a future spec starts using the second context.

## Integration: HTMX swap timing

**Claim**: `withHtmxSwap` "reads the swap counter *before* acting, so it cannot be satisfied by a
swap that had already happened."

**Verdict: MATCHES.** `withHtmxSwap` (`tests/integration/lib/htmx.ts`) reads the HTMX
settled/failed counters into a baseline, *then* calls the passed action, then waits for the live
counter to exceed that pre-action baseline - a swap that already happened before the call cannot
retroactively push the counter past a snapshot taken after it happened. The source file's own
docstring echoes the doc's sentence near-verbatim.

No action needed.

## Integration: write-quota retry logic

**Claim**: `ApiClient` retries a 429 up to three attempts, honoring `Retry-After` or DRF's body
delay; deliberately does not chase the hourly-scale ceiling; "the test timeout is 90s rather than
60s" because of this retry headroom.

**Verdict: MATCHES on the retry logic, STALE on the adjacent timeout number, doc updated.** The
core claim is exactly right: `ApiClient.send()` retries up to `THROTTLE_ATTEMPTS = 3`, honors
`Retry-After` then DRF's "available in N seconds" body text, and bails out once the quoted wait
exceeds `MAX_THROTTLE_WAIT_MS = 70_000` - well short of the hourly ceiling's thousands-of-seconds
delay, exactly the design intent the code's own comments state. But the specific number the doc
cited for the *consequence* of this design - "the test timeout is 90s" - no longer matches the
code: the actual default (`UL_E2E_TIMEOUT_MS`, `lib/env.ts`) is 180s, not 90s. Not part of the
audited claim's core mechanism, but found in the same pass and worth fixing since it was already
verified false.

**Fixed**: `docs/INTEGRATION_TESTS.md` now cites the real 70s retry cap and the real 180s default
test timeout (vs. Playwright's own 30s default), rather than the stale 90s/60s figures.

## Integration: production guard

**Claim** (`docs/INTEGRATION_TESTS.md` "The production guard"): the suite refuses to start when
`UL_E2E_BASE_URL`'s hostname is in `UL_E2E_PRODUCTION_HOSTS` (defaulting to the real hostnames),
matching on the exact hostname so `s1.dev.urbanlens.org` is not caught by an entry for
`urbanlens.org`; `provision_integration_env` independently refuses to run in production unless
*both* `--force` and `UL_ALLOW_INTEGRATION_PROVISIONING=true` are set.

**Verdict: MATCHES - the highest-stakes claim in either doc checks out.** The TS-side guard
(`tests/integration/lib/env.ts`) does exact array-membership matching (`Array.includes`, never a
substring/suffix check) against a lowercased hostname, defaults to the genuine production
hostnames (`urbanlens.org`, `www.urbanlens.org`, `app.urbanlens.org` - cross-checked against
`ALLOWED_HOSTS` in `settings/app.py`/`settings/base.py`), and runs at *module-import* time - before
Playwright even finishes evaluating its config, earlier than any hook. The Python-side
provisioning guard is a genuine `if force and override` (AND, not OR), and unlike the TS guard it
was already directly unit-tested, including the specific negative case that `--force` alone is
insufficient (`test_integration_provisioning.py`).

**The one real gap**: the TS-side hostname-matching logic - the specific mechanism protecting
against the exact scenario the doc calls out by name (`s1.dev.urbanlens.org` vs. `urbanlens.org`)
- had zero automated test coverage. It lived as inline code inside `env.ts`'s eager,
side-effecting startup block, which throws immediately at import time if `UL_E2E_BASE_URL` is
unset - making it unreachable by a normal test import without also satisfying the rest of that
module's environment validation.

**Fixed**: extracted the matching logic into a new pure function, `isProductionHost()`
(`tests/integration/lib/production-guard.ts`), with no import-time side effects; `env.ts` now
calls it instead of inlining the check (behavior unchanged - verified by inspection, since this is
a pure extraction with the same lowercasing/comparison). New test file
`tests/integration/specs/services/production-guard.spec.ts` (6 tests): exact match caught, the
doc's own named counter-example (`s1.dev.urbanlens.org` vs. an `urbanlens.org` entry) NOT caught,
an unrelated denylist entry NOT caught, case-insensitivity, an empty denylist catching nothing,
and every one of the three real default hosts genuinely caught by its own default list.

**Verification**: `tsc --noEmit` passes cleanly on all three changed/new files (confirmed via a
throwaway clone on the `chiron` dev host, since Node/TypeScript tooling isn't available on this
Windows checkout - see `CLAUDE.local.md`). Full execution via `npx playwright test` was not
achieved: this suite's `globalSetup` runs unconditionally before any project (even one requesting
zero fixtures), and it requires a live `UL_E2E_BASE_URL` deployment plus provisioned accounts to
complete - by the suite's own design, this is an on-demand suite meant to run against staging, not
something to spin up a deployment for just to execute one unit-style spec. The new test's
correctness rests on `tsc`'s type-check plus manual tracing of the (deliberately trivial) logic,
not a live pytest-equivalent run - noted here rather than overclaiming a green run that didn't
happen.

## Integration: known gaps still accurate

**Claim** (`docs/INTEGRATION_TESTS.md` "Known gaps"): each Playwright worker computes its own
`runId` independently (no cross-worker propagation), so `--purge` by run id can miss rows; the
wiki specs skip on a fresh deployment because the external API has no wiki-creation endpoint,
recorded as a `docs/PROBLEMS.md` finding dated 2026-08-24.

**Verdict: MATCHES.** `runId` (`tests/integration/lib/env.ts`) still falls back to a
clock-derived expression evaluated fresh per-import, with no mechanism (env override, global-setup
propagation) that gets a computed value into sibling worker processes - each worker genuinely gets
its own `resourcePrefix`. The `docs/PROBLEMS.md` "a native client can edit a wiki but can never
start one" entry is still tagged `OPEN`, and the external API's `WikiDetailApiView` still defines
only `GET`/`PATCH`, no `POST` - confirming there's still no wiki-creation route, so
`wiki.spec.ts`'s five dependent tests still legitimately skip on a fresh deployment, exactly as
both docs describe.

No action needed.

## Fixes applied (2026-08-25)

1. **`docs/CONTRACT_TESTS.md`'s "What the first run found" section read as an open-findings list
   for two issues fixed 2026-08-24.** See
   [Contract: operationId/401 findings](#contract-operationid401-findings). Doc-only fix, no code
   change (the underlying fix already existed and is already tested).
2. **`docs/INTEGRATION_TESTS.md` cited a stale test-timeout number** (90s, actual default 180s).
   See [Integration: write-quota retry logic](#integration-write-quota-retry-logic). Doc-only fix.
3. **The production-write guard's exact-hostname logic had zero automated test coverage.** See
   [Integration: production guard](#integration-production-guard). Not a bug - the guard already
   worked correctly - but the specific mechanism protecting against writing to production had
   nothing exercising it. Extracted to `tests/integration/lib/production-guard.ts`; 6 new tests
   in `tests/integration/specs/services/production-guard.spec.ts`. Type-checked clean; full
   Playwright execution needs a live deployment per the suite's own design and was not attempted.

## Open questions for Jess

- **`secondaryPage`'s missing `PageGuard`** (noted under
  [Integration: console guard](#integration-console-guard)) is currently inert - nothing uses that
  fixture yet - but worth deciding now rather than after the first spec that does use it ships
  without console-error protection: should `secondaryPage` get the same guard as `page` by
  default, or is an unguarded second context sometimes exactly what a spec needs (e.g. one
  deliberately testing an error page)?

## Not yet audited

Both docs cover more ground than the seven-plus-one topics checked this round. `docs/CONTRACT_TESTS.md`'s
remaining unaudited claims: the two-mode comparison table's specifics (in-process vs. live
differences beyond what was checked), the "detail operations mostly exercise the 404 path"
coverage-gap claim, and the coverage-phase (`UL_CONTRACT_COVERAGE`) behavior. `docs/INTEGRATION_TESTS.md`'s
remaining unaudited claims: the account-provisioning state claims (verified email, no
passkey/TOTP, the two-API-key setup), the malware-scanner-503/sign-in-lockout "not a defect"
claims, and most of the per-spec-file "question each spec is really asking" table.
