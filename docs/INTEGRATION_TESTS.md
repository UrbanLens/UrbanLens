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
| `visual` | Opt-in screenshot comparison (`UL_E2E_VISUAL=1`). |

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
| `ui/*` | Sign-in, the map, navigation, pin detail. |

> **Calibration status.** Everything except the seven files added on 2026-08-24
> (`api/labels`, `api/trips`, `api/undo`, `api/search`, `api/wiki`,
> `services/media-storage`, `ui/trips`) has been run against a live deployment
> and had its assumptions corrected. Those seven typecheck and register, and
> their payload shapes were read out of the generated OpenAPI document rather
> than guessed, but **they have not yet been run against a deployment** - so
> treat a failure from one of them as "the test is probably wrong" until a first
> calibration run says otherwise. The first run of the original suite corrected
> fifteen wrong assumptions, and there is no reason to think these are better.

Some things it deliberately does **not** do:

- **Throttling.** Proving the rate limiter works means tripping it, and tripping
  it on a shared staging box locks the next person out.
- **Payments.** Stripe is not exercised; a test that charges anything is not a
  test worth having on a schedule.
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
- **Seven spec files have never been run against a deployment.** Listed under
  "Calibration status" above. Running them is the next thing worth doing here.
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
