# Integration tests

An on-demand suite that drives a **deployed** UrbanLens instance over HTTP: the
real database, the real cache, the real Celery workers, the real WebSocket
container, the real proxy and the real TLS. It is run by hand against staging,
never automatically, and never against production.

It is not a replacement for the 11,000-test pytest suite. That one answers "does
this code do what it says". This one answers the question that suite structurally
cannot: **do the pieces work together on a machine where they are separate
processes?** Everything it checks is invisible from inside a single process -
a bundle that did not build, a proxy that will not upgrade a WebSocket, a worker
container that is not consuming the queue, a header a proxy rewrote, an asset
manifest that was never regenerated.

- **Where:** `tests/integration/`
- **Tool:** [Playwright Test](https://playwright.dev) (TypeScript)
- **Runner:** `bin/run_integration_tests.sh`

## Quick start

Two steps: provision accounts on the deployment, then point the suite at it.

```bash
# 1. On the deployment under test (inside the app container):
python src/urbanlens/manage.py provision_integration_env --out /tmp/e2e.json

# 2. From a checkout, anywhere that can reach it:
UL_E2E_ACCOUNTS_FILE=/tmp/e2e.json \
  bin/run_integration_tests.sh --url https://s1.dev.urbanlens.org
```

`--docker` runs everything in the official Playwright image, so nothing has to
be installed locally except Docker:

```bash
bin/run_integration_tests.sh --url https://s1.dev.urbanlens.org --docker
```

Afterwards, on a shared instance:

```bash
python src/urbanlens/manage.py provision_integration_env --purge --execute
```

## Nuclei

A template-driven vulnerability scanner, run against the same deployment as a
separate tool rather than a Playwright project - it answers a different
question than anything in `specs/security/`. Those specs assert specific
application behaviour (does another account's pin look like it never
existed); Nuclei checks the deployment against a catalogue of thousands of
known CVEs, exposed panels and files, default credentials, and misconfigured
headers. Neither replaces the other.

```bash
bin/run_nuclei_scan.sh --url https://s1.dev.urbanlens.org
bin/run_nuclei_scan.sh --url ... --docker              # no local install needed
bin/run_nuclei_scan.sh --url ... --fail-on-findings    # nonzero exit if anything matched
bin/run_nuclei_scan.sh --url ... --accounts-file /tmp/e2e.json  # authenticated
```

Same rules as the Playwright suite: manual only, and it refuses to run
against a production hostname (`UL_NUCLEI_PRODUCTION_HOSTS`,
`UL_NUCLEI_ALLOW_PRODUCTION`) for the same reason - this fires real requests,
some from templates tagged intrusive, at whatever it is pointed at. DoS-tagged
templates are excluded unconditionally, since it would impact other services on
the same machine.

The script preflights the target with a plain `curl` before handing anything
to Nuclei. Reason it exists: `-update-templates` is a one-shot maintenance
action in Nuclei - passed alongside `-u` (as the CI/CD guide's own examples
do) it updates the template catalogue, exits 0, and never scans anything. No
error, no warning, just a report with nothing in it, indistinguishable from a
hardened deployment genuinely tripping nothing. That is exactly what the
first live run against staging did: 0 findings, silently. Confirmed by
re-running the identical flags minus `-update-templates`, which found 22 -
mostly `info`-severity header and cookie hygiene, plus the already-tracked
missing-SRI finding from the integration suite's own first run (see "What the
first run found" above). The script now updates the template catalogue as a
separate step before scanning rather than combining the two, and the
reachability preflight exists so that a *genuinely* unreachable target (wrong
URL, no VPN, DNS) fails loudly instead of producing the same "0 findings, no
error" symptom for a different reason.

### Authenticated scanning

`--accounts-file` points at the same manifest `manage.py
provision_integration_env` writes for the Playwright suite (`--accounts-file
/tmp/e2e.json` after the Quick start above). On its own it authenticates
every request as the primary account's full-scope API key. Add `--all-tiers`
to scan **four times** instead of once:

```bash
bin/run_nuclei_scan.sh --url https://s1.dev.urbanlens.org --accounts-file /tmp/e2e.json --all-tiers
```

| Tier | Credential | What it reaches |
| --- | --- | --- |
| `unauthenticated` | none | The public perimeter |
| `apikey-restricted` | the `profile:read`-only key | The external API surface, at the narrowest scope a real integration would use |
| `apikey-full` | the full-scope key | The external API surface, every endpoint |
| `session` | a real signed-in cookie | The HTML/HTMX dashboard - maps, wiki, trips, admin |

These are not four passes over the same ground. `ExternalApiView` - the base
class every external API endpoint inherits - declares
`authentication_classes = [ApiKeyAuthentication, OAuth2Authentication]` with
no `SessionAuthentication`, so a session cookie cannot reach `/api/...` at
all; conversely the HTML/HTMX surface only recognises a session, so an API
key reaches none of it. `apikey-restricted` versus `apikey-full` is
redundant on generic infra-level templates (headers, TLS, tech fingerprints
show up identically at every scope) but not on anything scope-sensitive.
Each tier writes to its own `reports/nuclei/<tier>/` subdirectory rather than
one merged report, so a finding is traceable to the privilege level that
produced it.

A tier that cannot be set up - no restricted key provisioned, no Node
available for the session login - is skipped with a warning rather than
failing the run.

**The session tier signs in for real** rather than crafting a cookie by
hand: Django's CSRF-protected login (token, form POST, redirect chain) is not
worth reimplementing in bash when
`tests/integration/setup/auth.setup.ts` already does it correctly through a
real browser. The script runs that Playwright project itself (installing
Node dependencies and Chromium on demand), then lifts the `sessionid` /
`csrftoken` cookies out of the resulting `storageState` into a `cookie`-type
secret-file. Getting this working live against staging found two more real
bugs, neither specific to Nuclei:

- **Staging's `UL_SITE_URL` was wrong** (`http://localhost:21080` instead of
  `https://staging.urbanlens.org`), which meant `auth.setup.ts`'s own CSRF
  preflight - and therefore *every* Playwright project against staging, not
  just this one - was refusing to sign in at all. Fixed on the deployment.
- **A fresh account's first sign-in never completed.** `provision_integration_env`
  regenerates keys on every run (see its own docstring), so every freshly
  provisioned account hits `e2ee-client.ts`'s "save your recovery key" modal
  on first login - a blocking overlay `runLoginFlow` awaits before it ever
  navigates anywhere. `LoginPage.submitCredentials()` (test code, not
  application code) now dismisses it via the same "Remind me later" button a
  real user has, alongside the existing click+navigation race.

**A secret-file holds a live credential, and cleanup has to actually run.**
`--all-tiers --docker` originally shared one template-cache directory across
all four container runs to avoid re-downloading the catalogue four times.
Nuclei runs as root inside the container, so anything it wrote there came out
root-owned on the host - and because the EXIT trap's cleanup loop was a bare
`for` loop under `set -e`, the first path that failed to `rm -rf` (that
root-owned directory) aborted the loop before it reached the three queued
secret-files, leaving live API keys and a session cookie behind in `/tmp`.
Fixed two ways: cleanup swallows a failure per-path instead of aborting on
the first one (`rm -rf "$p" 2>/dev/null || true`), and the shared
template-cache mount is gone entirely - each tier re-downloads the catalogue,
slower but with no host-writable-by-root path for anything to leave behind.

In CI it is `.github/workflows/nuclei.yml`, dispatchable on its own or (the
default) alongside `integration.yml` via `run_nuclei: true` - set that input
to `false` on a dispatch to skip it. It reads the same `UL_E2E_ACCOUNTS_JSON`
secret on the `staging` environment that `integration.yml` uses, and runs all
four tiers whenever that secret exists (`authenticated: false` on a dispatch
to force an unauthenticated-only scan). Each tier's findings upload as their
own SARIF category to GitHub Code Scanning, plus one JSON Lines artifact
covering all of them; a finding is a lead to triage, not automatically a
broken build, so the job does not fail on one unless
`fail_on_findings`/`--fail-on-findings` is set.

The first full `--all-tiers` run's findings are triaged in docs/archive/PROBLEMS-ARCHIVE.md,
2026-08-28.

## sqlmap

[sqlmap](https://github.com/sqlmapproject/sqlmap) against the same deployment,
run by `bin/run_sqlmap_scan.sh`. It answers a question Nuclei, the contract
suite, and `specs/security/` all leave open: **is there an actual SQL
injection anywhere behind this API or these forms?** Nuclei matches known
patterns; the security specs assert that authorization holds; sqlmap sends
real payloads through real parameters into the real database and reports
whether one of them changed the query's behaviour. "What it deliberately does
not do" above already named the gap: `security/` does not run an active
scanner as a Playwright spec, and Nuclei and sqlmap are the two separate
on-demand tools that do instead. This section is the second one; a ZAP active
scan remains unassessed.

```bash
bin/run_sqlmap_scan.sh --url https://s1.dev.urbanlens.org
bin/run_sqlmap_scan.sh --url ... --accounts-file /tmp/e2e.json
bin/run_sqlmap_scan.sh --url ... --accounts-file /tmp/e2e.json --all-tiers
bin/run_sqlmap_scan.sh --url ... --fail-on-findings
bin/run_sqlmap_scan.sh --url ... -- --skip-waf --random-agent   # pass through
```

### Why this is stricter than Nuclei

Nuclei's templates are overwhelmingly detection: a fingerprint match, a header
check, a known-CVE probe. sqlmap's job is to prove an injection exists by
**exploiting** it - at `--risk=3` (this wrapper's default) that includes
OR-based payloads that can rewrite an `UPDATE`/`DELETE` statement's `WHERE`
clause to match every row, and the default `--technique` includes stacked
queries, which can run arbitrary follow-up SQL if the DBMS/driver stack
permits it. That is a different order of consequence than a scanner noticing a
missing security header, and it changes what "safe to point this at" means:

- **Target scope is an allowlist, not a denylist.** Every other tool here
  (`run_integration_tests.sh`, `run_nuclei_scan.sh`, `run_contract_tests.sh`)
  refuses a short list of production hostnames and otherwise trusts whatever
  URL it is given, including `staging.urbanlens.org`. `run_sqlmap_scan.sh`
  inverts that: it only runs against `UL_SQLMAP_ALLOWED_HOSTS` (default
  `.dev.urbanlens.org,localhost,127.0.0.1`) and refuses everything else,
  staging included, unless `UL_SQLMAP_ALLOW_ANY_HOST=1` is set. Dev containers
  on chiron are disposable - rebuildable from nothing - which is what makes
  `--risk=3` and stacked queries an acceptable default in the first place;
  staging is not disposable in the same way, and this wrapper does not trust
  an operator to remember that on a bad day.
- **A fixed set of flags is refused unconditionally**, regardless of how they
  are passed - not even behind an opt-in inside this wrapper: `--os-shell`,
  `--os-pwn`, `--os-cmd`, `--os-smbrelay`, `--os-bof`, `--priv-esc`,
  `--file-read`, `--file-write`, `--file-dest`, `--sql-shell`, `--udf-inject`,
  and the `--reg-*` Windows-registry family. All of them go past confirming an
  injection into operating-system command execution, arbitrary filesystem
  access on the database host, or an interactive shell that bypasses `--batch`
  entirely. Confirming the injection exists is this suite's job; going further
  is a deliberate, individually-run sqlmap command outside it, on a target you
  control. This mirrors Nuclei's unconditional exclusion of DoS-tagged
  templates - the same posture, applied to the flags that matter for this tool.

Everything else is deliberately permissive by default - full `--risk=3
--level=5`, sqlmap's own default `--technique=BEUSTQ` - matching Nuclei's
"exclude only what is unconditionally unsafe" posture, because the allowlist
above is what makes that safe here.

### sqlmap is not a project dependency

sqlmap publishes no checksums, signatures, or attestation for any release -
confirmed against every GitHub Release (`assets: []` on all of them) and the
repository history. It does, however, publish the *same* tagged release to
PyPI itself, so `bin/install_sqlmap.py` pins an exact version and both of
PyPI's own per-file SHA256 digests in `bin/sqlmap-requirements.txt`, and
installs with `pip install --require-hashes` - a supply-chain guarantee at
least as strong as vendoring a git commit SHA, with none of the custom
download-and-verify code that would otherwise need writing and trusting.

It installs into its own throwaway `.sqlmap/venv` (gitignored), never the
project's own `.venv`/`.venv_windows` - every contributor who runs `ruff` or
`pytest` installs the main dependency set, and sqlmap is an external scanner
this wrapper shells out to, the same relationship Nuclei has as a separately
installed (or dockerized) binary. Bumping the pin means updating the version
and both hashes in `bin/sqlmap-requirements.txt` together;
`--require-hashes` fails closed on a partial update.

### Target derivation: sqlmap's own `--openapi`, not a hand-built generator

The first draft of this wrapper generated targets by running the contract
suite (`tests/contract`) against the deployment with request/response capture
turned on, then converting the capture into raw requests for sqlmap's `-r`.
That duplicated work sqlmap already does better: `--openapi=<url>` parses a
schema directly (confirmed against sqlmap 1.10.8's own source,
`lib/parse/openapi.py`), synthesizes realistic values from each parameter's
declared example, fills path/query/header/cookie parameters and JSON/form
bodies, and marks every value it derives as a candidate injection point - one
flag instead of a bridge between two test suites. `run_sqlmap_scan.sh` points
it at `${BASE_URL}/dashboard/api/external/v1/schema/?format=json` - the exact
document `tests/contract/schema_source.py` fetches for the same reason: it is
what third parties (the Flutter app included) actually generate clients from.

**Known limitation, shared with the contract suite** (see "Detail operations
mostly exercise the 404 path" in docs/CONTRACT_TESTS.md): neither tool seeds a
*real* object identifier into a detail-view path parameter, so
`pins/{pin_slug}/`-shaped operations are tested against a slug that does not
exist. A slug-lookup injection on a genuinely matched row is not exercised by
either suite yet. Recorded here rather than solved here, for the same reason
the contract suite left it recorded rather than solved.

### The four tiers

`--all-tiers` (needs `--accounts-file`) runs sqlmap four times, mirroring
Nuclei's tiers because the underlying reachability split is identical - see
"Authenticated scanning" above for the full explanation of why an API key and
a session reach disjoint route surfaces rather than overlapping ones:

| Tier | Mechanism | What it reaches |
| --- | --- | --- |
| `unauthenticated` | `--openapi`, no credential | The public perimeter of the external API |
| `apikey-restricted` | `--openapi` + the `profile:read`-only key | The external API at the narrowest scope a real integration would use |
| `apikey-full` | `--openapi` + the full-scope key | The external API, every documented operation |
| `session` | `--crawl`/`--forms` + a real signed-in cookie | The HTML/HTMX dashboard - maps, wiki, trips, admin |

The `session` tier does not use `--openapi` at all: `ExternalApiView`
declares `authentication_classes = [ApiKeyAuthentication, OAuth2Authentication]`
with no `SessionAuthentication` (see "Authenticated scanning" above), so a
session cookie cannot reach `/api/...`, and the API-key tiers' schema has
nothing to say about the HTML surface. It crawls the dashboard instead
(`--crawl`, `--forms`, `--csrf-token=csrfmiddlewaretoken` for Django's
CSRF-protected forms) and signs in through the same real browser flow
(`tests/integration/setup/auth.setup.ts`) Nuclei's session tier already
established as the only correct way to get a Django session outside a test
client.

**No credential ever reaches argv.** Bearer tokens and session cookies are
written into a minimal sqlmap config file (`-c`, `[Request]` section only -
`authtype`/`authcred` or `cookie`) rather than passed as `--auth-cred`/
`--cookie` CLI flags, cleaned up in the same `trap cleanup EXIT` pattern
Nuclei's secret-files use, and for the identical reason: a live key or
cookie in a CLI argument is visible to anything else that can list processes
on the same box.

### Reading a finding

sqlmap's own exit code is not a findings signal - confirmed against its
source (`sqlmap.py`'s `os._exitcode`): it stays `0` on a completed scan that
found nothing, and is only ever `1` on sqlmap's own runtime error. Every run
instead carries `--report-json`, whose `data` array holds a `TARGET`- or
`TECHNIQUES`-typed entry only when sqlmap actually confirmed an injection
point; `run_sqlmap_scan.sh` counts those and prints a per-tier summary the
same way `run_nuclei_scan.sh` counts JSONL lines. `--fail-on-findings` turns a
nonzero count into a nonzero exit; off by default, since a finding here is a
lead to triage rather than automatically a broken build.

Reports land in `tests/integration/reports/sqlmap/` (sqlmap's own
`--output-dir` tree, including its full transcript log and, for anything
actually dumped, the row data itself - handle those the way `docs/PROBLEMS.md`
handles any other confirmed vulnerability), the same tree Nuclei and the
Playwright suite use, so all three are picked up by one CI artifact upload.

### What the first live calibration run found

Calibrated against a real dev container on chiron (`--all-tiers`, scoped to
the `pins`-tagged operations for a tractable run rather than all ~200). It
found three real bugs - none of them findings about UrbanLens's own SQL
safety, all of them things a fake local target could not have surfaced:

- **Every `--openapi` tier crashed, unconditionally, on first contact with a
  real deployment.** sqlmap's own `_setStdinPipeTargets()`
  (`lib/parse/cmdline.py`) treats *any* non-tty stdin as a potential piped
  target list - true for literally every way this wrapper is ever run
  (`</dev/null`, backgrounded, cron, CI) - and unconditionally overwrites
  `kb.targets` with its own lazy reader before `_setOpenApiTargets()` ever
  runs, crashing with `TypeError: object of type '_' has no len()` and never
  reaching `--report-json`. Worse than a loud crash: `count_findings()`'s own
  "no report.json yet" fallback reads as a clean 0-finding scan unless the log
  itself is read - exactly the "0 findings, no error" trap
  `run_nuclei_scan.sh`'s own history warns about, confirmed here to also catch
  sqlmap. Fixed with `--ignore-stdin`, sqlmap's own documented escape hatch for
  this, added to every invocation in the script.
- **The session tier refused to start at all**: sqlmap refuses to combine
  `--csrf-token` with `--threads` greater than 1
  (`lib/core/option.py: if conf.csrfToken and conf.threads > 1`). Fixed by
  hardcoding `--threads=1` for that tier specifically, regardless of `--threads`.
- **A real, reproducible information-disclosure bug in the application
  itself**, found because sqlmap's WAF-bypass mode sends browser-like headers
  (`Accept: text/html,...`) that a normal API client wouldn't: any request to
  the external API that content-negotiates to HTML crashed with
  `TemplateDoesNotExist: rest_framework/api.html` - `rest_framework` was never
  added to `INSTALLED_APPS`, so DRF's default `BrowsableAPIRenderer` (on by
  default alongside `JSONRenderer` whenever `DEFAULT_RENDERER_CLASSES` isn't
  overridden) can never find its own template. In an environment where `DEBUG`
  resolves true, that 500 came with a full Django debug page in the body -
  settings values (internal Valkey/Redis hostnames, CSP configuration),
  traceback, the works. Fixed in `REST_FRAMEWORK` (`settings/base.py`) by
  setting `DEFAULT_RENDERER_CLASSES` to `JSONRenderer` only - correct
  independent of the crash, since this is a machine-consumed API with no
  working browsable UI to begin with (the working interactive explorer is the
  separate Swagger UI view). Verified live: `Accept: text/html` now answers a
  clean `406` instead of a 500.

No confirmed SQL injection in the scope calibrated so far - the ~200
operations outside `pins` remain unexercised at the time of writing.

## Why Playwright

The alternative considered was `pytest-playwright`, which would have kept
everything in one language. It was rejected for a specific reason rather than a
stylistic one: **against a remote deployment there is no ORM**. The whole
advantage of staying in Python - `TestCase`, Model Bakery, fixtures, direct
model access - is unavailable here, because the database is on another machine
behind an application this suite is only allowed to talk to over HTTP. With that
gone, what is left to choose on is the runner, and Playwright's own is
substantially better equipped for this shape of work: per-test browser
isolation, worker-level parallelism, project dependencies, auto-retrying
assertions, the trace viewer, and built-in screenshot comparison. The repository
already carries TypeScript, `tsconfig.json` and a bun-run TS test suite, so this
is not a new language in the project either.

## What it covers

Each project is selectable with `--project`, and they are ordered by how much
they tell you when they fail.

| Project | What it answers |
| --- | --- |
| `smoke` | Is this deployment alive, and does every page still render? |
| `services` | Are the dependencies - Valkey, Celery, Channels, static pipeline, CDNs - actually working? |
| `api` | Does the published external API still honour its contract? |
| `ui` | Do the real user journeys work in a real browser? |
| `a11y` | Does anything on the main pages fail a WCAG AA check at serious or above? |
| `security` | Do private rows stay private, do sessions stay bound, does user input stay data? |
| `visual` | Opt-in screenshot comparison (`UL_E2E_VISUAL=1`). |
| `location` | Opt-in live location-data specs for one real place (`UL_E2E_LOCATION_DATA=1`). Slow, and they spend real money at REData, EPA ECHO and Wikipedia - see `docs/LOCATION_DATA_TESTS.md`. |

A run with no `--project` does everything except `visual` and the non-Chromium
browsers.

Domains covered, and the question each spec file is really asking:

| Spec | The question |
| --- | --- |
| `api/pins` | Does the shared creation path behave against real data (dedup, slugs, tombstones)? |
| `api/labels` | Are the opt-in counts present exactly when asked for, and is the name uniqueness rule in front of the database constraint? |
| `api/trips` | Does a trip → activity → pin → location join survive being read back, including through the trip map? |
| `api/undo` | Does deleting something leave a restorable entry, and does restoring it work? |
| `api/search` | Does a thing made seconds ago turn up, and did any search source fail behind a 200? |
| `api/wiki` | Does `base_revision_id` actually stop a concurrent edit from overwriting one, or is it only declared? |
| `api/auth`, `api/contract` | Scope enforcement, and the published schema itself. |
| `services/media-storage` | Do uploaded bytes survive leaving the process, and does the URL handed back actually serve them? |
| `services/*` | Valkey, Celery, Channels, the static pipeline, headers, third-party origins. |
| `ui/trips` | Does a trip page render the join, including the empty case? |
| `ui/*` | Sign-in, the map, navigation, the Private Pin page. |
| `security/assumptions` | Are the two accounts two people, and can each still read their own rows? (If this is red, ignore the rest of `security/`.) |
| `security/authorization` | Does another account's pin, list, trip, label, filter, photo, check-in or undo entry look like it never existed - and can they not write it either? |
| `security/isolation` | Do search, the map JSON, HTML, photo bytes and unearned wikis stay scoped to the caller? |
| `security/session` | Are session cookies HttpOnly, is CSRF bound to origin, and does `next=` stay on-site? |
| `security/disclosure` | Do 404s, `.git`, `.env`, whoami, settings and staff surfaces keep secrets and stack traces to themselves? |
| `security/input` | Do stored descriptions/comments/notes stay text, and do search/link/path inputs refuse to become HTML, SQL or files? |
| `security/transport` | Do CORS, Host and method-override stay conservative through the real proxy? |
| `security/surfaces` | Is `/dashboard/rest/` session-only, are API keys Bearer-only, is `/media/` gated, and are exports/webhooks/password-reset not oracles? |
| `location/hrsh-boundary` | Does a parcel boundary ever arrive for a real place, and reach the map? |
| `location/hrsh-boundary-provenance` | Did that boundary come from a *provider*, or did we invent it? Presence and provenance are separate questions, and the first spec passes while the second fails - see `docs/LOCATION_DATA_TESTS.md`. |
| `location/*` | Live third-party data for one real place: place identity, buildings, wiki, media, property records, panels. Opt-in. |

> **Calibration status.** Every spec here has now been run against a live
> deployment. The seven added on 2026-08-24 were calibrated the same day: their
> first run corrected four assumptions (`with_counts` is a list concern, a
> duplicate label name answers 409, a non-image upload answers 409, and the
> `types` search filter takes a token the schema does not enumerate), and
> exposed one thing about the *suite* rather than the app - see "Running it
> more than once" below.

## Running it more than once

The external API caps a credential at **60 writes a minute** and **300 an
hour**. The suite is the burstiest client it will ever have: nearly every spec
that needs a pin creates one, in parallel, on a single key.

`ApiClient` handles the per-minute cap for you - it waits out a 429 and retries,
honouring `Retry-After` or the delay DRF puts in the body, up to three attempts, capped at 70s of
total wait (`MAX_THROTTLE_WAIT_MS`, `lib/api-client.ts`) before giving up and returning the 429 as
one. That headroom is part of why the default per-test timeout (`UL_E2E_TIMEOUT_MS`, `lib/env.ts`)
is 180s rather than Playwright's own 30s default. It deliberately does *not*
chase the hourly ceiling: if 300 writes are gone the wait is tens of minutes,
and the honest answers are to run less often, or to provision a second account
and point a second run at it.

A full run costs roughly a hundred writes, so **three back-to-back runs inside
an hour will exhaust the hourly quota** and start failing on writes with no way
to distinguish that from a broken endpoint. Tell the two apart by the number in
the message: the per-minute cap quotes *seconds* ("available in 3 seconds") and
the client rides it out; the hourly cap quotes *thousands* of seconds
("available in 2017 seconds") and every write for the next half hour fails.

Two other limits show up the same way - as failures that look like defects and
are not:

- **The malware scanner.** Every upload is scanned before it is stored, and the
  API answers 503 when the scanner is unreachable. That is correct behaviour,
  and it is not a statement about whatever the test was checking, so the photo
  specs skip on it rather than failing. A scanner that is genuinely down is
  `services/dependencies.spec.ts`'s business.
- **Sign-in lockout.** Repeated runs in quick succession can trip the login
  rate limit, and the form then reports "Please enter a correct username and
  password" for a password that is perfectly correct. Re-provisioning does not
  clear it; waiting does.

The way out of the write quota, when it happens, is to **re-provision**:

```bash
python src/urbanlens/manage.py provision_integration_env --out /tmp/e2e.json
```

The throttle is keyed per credential, and provisioning revokes the old keys and
mints new ones, so the fresh key starts with a fresh quota. This is a
maintainer verifying a deployment, not a way around a limit that exists to stop
abusive clients - if you find yourself doing it repeatedly, the run is too
write-heavy and the fix is to share fixtures between specs.

Some things it deliberately does **not** do:

- **Throttling.** Proving the rate limiter works means tripping it, and tripping
  it on a shared staging box locks the next person out.
- **Payments.** Stripe is not exercised; a test that charges anything is not a
  test worth having on a schedule. The security project does hit the webhook
  path unsigned, and asserts it is refused.
- **Attack scanners**, mostly. The `security` project is assertions about
  refusals, identical 404s, cookie flags, and markup that must not become DOM.
  It does not run an active scanner against the deployment itself - **Nuclei
  and sqlmap are the two exceptions**, both separate on-demand tools rather
  than Playwright specs, see below. A ZAP active scan remains unassessed.
- **External providers.** Provisioned accounts have `external_apis_enabled` and
  `ai_enabled` set to False, so no provider is billed by a test run. Pass
  `--external-apis` when provisioning if you specifically want to exercise the
  REData-backed panels.

## The accounts

A deployed instance cannot be tested with fixtures, and it cannot be tested by
signing up: `RegistrationForm.save` leaves `is_active` False pending a
verification email, and nothing in a headless run can click the link. So the
accounts are provisioned by a management command instead.

`provision_integration_env` creates or refreshes one account per role
(`primary` and `secondary` by default) and puts each in the state a headless run
needs:

- active, with a verified email record, so sign-in is not refused;
- past welcome onboarding and profile setup, so `PostLoginRedirectView` does not
  divert the run to a wizard;
- no passkey, no TOTP and no derived-auth salt, so the password in the manifest
  is what the login form actually posts;
- outbound APIs and AI off, and every notification set to on-site delivery, so a
  run bills nothing and mails nothing;
- two API keys - one with every scope, and one deliberately holding only
  `profile:read`, because the only way to prove scope enforcement is a
  credential that is valid and insufficient.

Accounts are identified by **both** the `e2e-` username prefix and an
`@e2e.invalid` address. Neither is reachable by an ordinary sign-up: the
username validator forbids `-`, and RFC 2606 reserves `.invalid` so no mail can
ever be delivered there. `--purge` requires both, plus the account not being
staff. That is stricter than `purge_demo_accounts`, which selects on a username
prefix alone - the demo runs against a database holding nothing else, while this
may be pointed at a staging instance people also use by hand.

The manifest it writes contains **plaintext passwords and API keys**. `--out`
writes it to a file (mode 600) rather than to a terminal by default.

### Provision after the instance has a real admin

`promote_first_user_if_needed` grants site admin to the first user created on a
fresh site, and the slot is single-claim and permanent. The integration prefix
is excluded from it, alongside the demo prefix and for the same reason - a
throwaway account holding that slot would leave the real operator unable to be
promoted, and a purge would leave the slot pointing at a deleted row.

The consequence is worth knowing: on a genuinely empty instance, provisioning
first leaves the slot **unclaimed**, and whoever registers afterwards is no
longer the first user, so nobody is auto-promoted. Provision after the operator
account exists, or promote one deliberately with `createsuperuser`.

## Writing a test

Everything a spec needs arrives as a fixture, and everything a spec creates is
removed afterwards whether it passed, failed or timed out.

```ts
import { expect, test } from "../../lib/fixtures.js";

test("a pin the API created is visible on its detail page", async ({ page, api }) => {
    const pin = await api.createPin();          // deleted in teardown
    await page.goto(`/dashboard/map/pin/${pin.slug}/`);
    await expect(page.locator("#pin-detail-hero")).toContainText(pin.name);
});                                             // console errors asserted here
```

The fixtures:

| Fixture | What it is |
| --- | --- |
| `page` | Already signed in as `primary`, with HTMX tracking and the console guard attached. |
| `api` | External-API client as `primary`. Anything it creates is tracked and deleted. |
| `restrictedApi` | Same account, `profile:read` only - for scope-enforcement assertions. |
| `secondaryApi` | External-API client as `secondary` - for "what can one user see of another". |
| `anonymousApi` | No credentials, for authentication assertions. |
| `secondaryPage` | A second signed-in browser context. |
| `guard` | The console/network watcher, for narrowing an expected error. |
| `account` | The credentials and scopes the run is using. |

And the helpers, all under `lib/`:

| Helper | Module | For |
| --- | --- | --- |
| `withHtmxSwap`, `clickAndSwap`, `waitForHtmxSettled` | `htmx.ts` | Waiting for a fragment to actually arrive and be swapped in. |
| `expectToast`, `expectNoErrorToast`, `dismissToasts` | `toasts.ts` | Reading what the server decided. |
| `probeWebSocket`, `observePageSockets` | `websocket.ts` | The Channels surface. |
| `expectAccessible`, `scanAccessibility` | `a11y.ts` | axe scans with the severity policy applied. |
| `publicRoutes`, `appRoutes`, `optionalRoutes`, `pinDetail(slug)` | `routes.ts` | Addressing pages without string literals. |
| `LoginPage`, `AppShell`, `MapPage`, `PinDetailPage` | `pages/` | The surfaces with more than one assertion against them. |

### Set state up over the API, assert through the UI

Driving the map form to get a pin on screen tests pin creation, not the thing
under test, and costs seconds every time. Creating the row over the API and
asserting on the rendered page is faster *and* a sharper failure signal: when it
fails it is because rendering broke, not because a toolbar moved.

### Wait for HTMX explicitly

Most of this application updates itself through HTMX, and Playwright's
auto-waiting does not cover "the fragment I asked for has come back and been
swapped in". Without an explicit wait, an assertion races the swap and fails
about one run in ten - the single largest source of flake in a suite like this.

```ts
import { withHtmxSwap } from "../../lib/htmx.js";

await withHtmxSwap(page, () => saveButton.click());
```

It reads the swap counter *before* acting, so it cannot be satisfied by a swap
that had already happened.

### Assert on the toast

`CLAUDE.md` states the rule these helpers encode: results and errors surface as
toasts. That makes a toast the most reliable evidence of what the server
actually decided - more reliable than re-reading the page, because a failed
action usually leaves the page looking exactly as it did before. Toasts expire
after 4.5 seconds, so assert promptly.

```ts
import { expectToast, expectNoErrorToast } from "../../lib/toasts.js";

await expectToast(page, "success", /saved/i);
```

### The console guard

Every UI test fails if its page logged a console error, threw, or failed to
fetch a subresource. "The heading rendered" is a weak claim when a script threw
before it could wire the page up.

A spec that legitimately provokes one narrows it:

```ts
guard.allow(/tools\/export\/status\//);
```

Turn the check off entirely (`test.use({ strictConsole: false })`) only for a
spec whose subject *is* third-party markup - the Swagger UI page is the one
current example.

### Never sign out on the shared session

Every project reuses one saved session per role. Django's logout flushes that
session **server-side**, so signing out against the shared cookie signs out
every test running in parallel, and they fail with "redirected to login" for
reasons nothing in their own code explains. `specs/ui/authentication.spec.ts`
therefore does its own sign-in in a context of its own, and so must anything
else that ends a session.

## Configuration

Everything is environment variables; `tests/integration/.env.example` is the
full list, and a `.env` beside it is read if present. The three that matter:

| Variable | Purpose |
| --- | --- |
| `UL_E2E_BASE_URL` | The deployment under test. Required. |
| `UL_E2E_ACCOUNTS_FILE` | The manifest `provision_integration_env` wrote. |
| `UL_E2E_IGNORE_HTTPS_ERRORS` | For a staging box with its own certificate authority. |

### The production guard

The suite refuses to start when `UL_E2E_BASE_URL`'s hostname is in
`UL_E2E_PRODUCTION_HOSTS` (which defaults to the real hostnames). It writes and
deletes rows as a real account, and the mistake is one environment variable
wide. Matching is on the exact hostname, so `s1.dev.urbanlens.org` is not caught
by an entry for `urbanlens.org`.

The provisioning command has its own, independent guard: it refuses to run when
`UL_ENVIRONMENT` is `production` unless **both** `--force` is passed and
`UL_ALLOW_INTEGRATION_PROVISIONING=true` is set. Two locks, because each covers
a different mistake - a command typed in the wrong terminal, and a script that
has always carried `--force` being pointed somewhere new.

## Reading a failure

Reports land in `tests/integration/reports/`:

- `reports/html/` - the HTML report, with the failing step, its screenshot and
  its trace. `npm --prefix tests/integration run report` opens it.
- `reports/junit.xml`, `reports/results.json` - for anything that consumes them.
- `reports/artifacts/` - traces and videos.

A trace is the thing worth opening: it replays the run with the DOM, the network
log and the console at every step.

```bash
npx playwright show-trace tests/integration/reports/artifacts/<test>/trace.zip
```

Retries are on (once) by default, because a shared staging box has real network
variance. `UL_E2E_RETRIES=0` turns them off when chasing an intermittent
failure, and `UL_E2E_HEADED=1 UL_E2E_SLOW_MO_MS=250 --project=ui -- --grep "name"`
runs one spec where it can be watched.

## What the first run found

The suite was calibrated against a real deployment (a dev-environment stack
built from `feat/multi-site-health-probes`) rather than being written and left
untried. That run is why several specs assert what they assert - a handful of
them were wrong in ways only a real deployment could show.

**It ends at 105 passing and 13 failing, and every one of the 13 is a genuine
finding rather than a suite defect.** Expect them on the first run somewhere
else too, and treat them as a baseline to work down:

| Failing test | Finding |
| --- | --- |
| `a11y` × 10 | **`<html>` has no `lang` attribute.** `themes/base.html` and `themes/auth_base.html` both open `<html id="html-root">`. WCAG 3.1.1, serious, every page. A screen reader guesses the language. |
| `services › external scripts are pinned with SRI` | **HTMX is loaded from `unpkg.com` with no `integrity`.** jQuery and toastr next to it have one. Without it, whoever controls that CDN controls this application. |
| `services › the server does not advertise what it is running` | nginx sends `Server: nginx/1.31.3`. One line at the proxy (`server_tokens off;`) removes it. |
| `ui › the map is usable at phone width` | The map page scrolls sideways by 40px at 390px wide. |

One further finding arrived as an *intermittent* failure, which is how the
console guard earns its place: opening a freshly created pin's detail page
sometimes fetches `.../wikipedia/` and `.../comments/` and gets **404** from
both. `themes/base.html` toasts every non-2xx HTMX response, so a user sees
error toasts on a pin they just made. It passed on retry, so it is a race
rather than a constant.

Colour-contrast violations are real and widespread too, but they are routed to
advisory rather than failing (see `ADVISORY_RULES` in `lib/a11y.ts`) - a project
that is red on every run from the day it is written gets muted, and then catches
nothing. They still appear in each report's `a11y-advisory.txt`.

## Known gaps

Recorded so they do not have to be rediscovered:

- **Every worker invents its own run id, so `resourcePrefix` is not one value.**
  `lib/env.ts` derives `runId` from the clock when `UL_E2E_RUN_ID` is unset, and
  each worker is a separate process that imports it again. `env.resourcePrefix`
  is `e2e-${runId}`, so a four-worker run stamps four different prefixes on the
  rows it creates - which means the promise above, that an interrupted run's
  leftovers are greppable by run id, does **not** currently hold, and
  `--purge` selecting on a single prefix can miss rows.

  Setting the variable from `playwright.config.ts` does not fix it: Playwright
  snapshots the environment before loading the config, so the mutation never
  reaches a worker (verified by reading `/proc/<worker>/environ`). The fix is to
  export `UL_E2E_RUN_ID` in the shell that launches the run, or to set it from
  `globalSetup` and confirm it propagates. Until then, purge by the `e2e-`
  prefix and the `@e2e.invalid` address rather than by run id.

  Found while chasing why `specs/location/` took ninety minutes: its expensive
  worker-scoped fixture cached its result per run id and never got a hit.

- **Celery is probed, not inspected.** `specs/services/background-jobs.spec.ts`
  runs a real data export and waits for it to finish, which proves the broker,
  the worker and the result path together. There is no queue-depth or
  worker-count assertion, because nothing serves one over HTTP. Adding Celery to
  `/health/ready` would change what readiness means, which is a deployment
  decision rather than a testing one.
- **The visual baselines are not committed yet.** The first
  `--project=visual --update-snapshots` run against a stable staging instance
  creates them; until then that project has nothing to compare against. It is
  also the one project the calibration run did not exercise.
- **No lockfile.** Direct dependencies are pinned exactly, so runs are
  reproducible to the version; transitive ones float. Committing the
  `package-lock.json` from a first `npm install` closes that.
- **Messaging behaviour cannot be exercised at all.**
  `permissions.OAUTH2_ONLY_SCOPES` restricts `messages:read`/`messages:write` to
  user-consented OAuth2 tokens, so a PAT-style API key is refused across the
  entire messaging surface - by design, so that a bearer key in a CI config is
  never a way into somebody's conversations. The suite authenticates with API
  keys and has no way to drive an authorization-code flow, so
  `api/messages.spec.ts` asserts the *closure* instead: every messaging
  endpoint must answer 403 to a key whose own scope list names those scopes.
  Testing what messaging actually does needs an OAuth2 client added to the
  provisioning command first.
- **The wiki specs skip on a fresh deployment.** A wiki is promoted through the
  web UI and the published API has no endpoint that creates one, so the suite
  cannot make the precondition it needs through the surface it is testing. They
  resolve the wiki and skip with that reason; they start running the moment one
  exists. Recorded as a finding in `docs/PROBLEMS.md`, 2026-08-24.
- **A photo vote's success path cannot be exercised at all.**
  `POST photos/{uuid}/vote/` only accepts a photo carrying the
  `(location, media_source_key, media_item_key)` identity that
  `services.media.media_materialize.materialize_media_item` sets when an
  externally-sourced Media gallery item is sent to a wiki - a plain upload
  never has it, even once shared to a wiki (see `PhotoVoteView`'s docstring).
  That materialization only happens through the web UI's wiki Media gallery
  (`WikiMediaVoteView`), which the published API has no equivalent of, so
  `api/photo-metadata.spec.ts` can only assert the refusal, not a successful
  vote-and-withdraw.
- **Games (SpotGuessr, Trivia, Consensus) are uncovered.** Each is
  WebSocket-driven with its own session lifecycle and deserves its own spec
  file; the socket helpers to write them are in `lib/websocket.ts`. They are
  also alpha-gated, so a provisioned account cannot reach them without the alpha
  flag.
- **Two specs are timing-sensitive against a slow deployment** and lean on the
  single retry: "a row written through the API is visible through the web UI"
  and "renders its own map". Both wait on a page that pulls Leaflet from a
  public CDN.

## Pointing it at the right URL

Django trusts only the origins it was configured for (`ALLOWED_HOSTS` plus
`UL_SITE_URL`). Reaching an instance by a *different* origin - through a
published container port rather than its hostname, say - leaves every page
rendering perfectly and every POST refused with a CSRF 403, sign-in included.

The preflight catches this: the provisioning manifest carries the deployment's
own `SITE_URL`, and `global-setup.ts` compares it against `UL_E2E_BASE_URL` and
refuses to start on a mismatch, naming both. If you genuinely need to reach a
deployment by another URL, set `UL_SITE_URL` on the deployment to match and
restart it.
