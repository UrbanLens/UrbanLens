# N28 — A CSP violation shows up in the app log as `CSP violation:` and as a `[csp]` problem in the browser suite; fix it at the source, never with a wildcard

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

`id: N28` · `status: current` · `updated: 2026-09-23`

The site policy is enforced by default since P143 (2026-09-23). A violation now means something on
the page did not load or did not run. Usually nothing visible happens: a dialog stays open, a map
tile layer stays blank, or a sign-in button does nothing.

## Where the policy lives

- `src/urbanlens/UrbanLens/settings/base.py`, `_CSP_DIRECTIVES`. Origins that depend on
  configuration are added by `allow_vendor_mirror`, `allow_media_origin` and
  `allow_basemap_style_origins`.
- `UL_CSP_ENFORCE=false` switches the header to `Content-Security-Policy-Report-Only`. Use it only
  while a breaking violation is being fixed.
- Some responses set their own policy, which the middleware does not overwrite: proxied media
  (P139), the article Sources PDF, and nginx's media locations. Basemap tiles are exempt. The OAuth
  consent page (`controllers/oauth_authorize.py`) adds the client's redirect URI to `form-action`.

## Finding the violation

1. **Production or staging:** grep the app logs for `CSP violation:`. The browser posts each
   violation to `report-uri /csp-report/`, and `controllers/csp_report.py` logs it as
   `<directive> refused <blocked> on <document> (source <file>, <disposition>)`. Credentials, query
   strings and fragments are removed from URLs before logging. Each address is limited to 60 reports per five minutes.
2. **Locally:** open DevTools. Chrome logs most violations to the console. A caught `eval` logs
   nothing there, so also run
   `document.addEventListener("securitypolicyviolation", e => console.log(e.effectiveDirective, e.blockedURI, e.sourceFile, e.lineNumber))`
   before you reproduce it.
3. **In the browser suite:** the page guard (`tests/integration/lib/page-guard.ts`) records every
   `securitypolicyviolation` a document raises, enforced or report-only, as a `[csp]` problem. The
   spec that caused it fails. `specs/security/csp.spec.ts` checks that the guard still catches
   violations. A violation inside a worker (a MapLibre tile worker fetching a host that
   `connect-src` does not list) fires on the worker's scope, so the guard does not see it. This
   session did not check whether the browser still sends it to `/csp-report/`.

`blocked` is a URL or a keyword. `eval` and `wasm-eval` mean code compiled from a string. `inline`
means an inline script or style. `blob` and `data` are URL schemes.

## Fixing it at the source

- **A new third-party origin:** add that origin to the one directive that needs it. Do not add
  `https:` or `*`. Remember that MapLibre fetches tiles, styles and glyphs through `connect-src`,
  while Leaflet loads tiles as images through `img-src`.
- **`eval` from htmx:** htmx 1.9 compiles `hx-on`, `js:` `hx-vals` and `hx-trigger` filters
  (`event[...]`) from strings. Use `data-ul-on-success` / `data-ul-after-request` /
  `data-ul-before-request` actions (`frontend/ts/shared/htmx-actions.ts`), `data-ul-lazy-section`
  (`collapsible-sections.ts`), `data-ul-min-query`, or an `htmx:configRequest` listener.
  `htmx-actions.contract.test.ts` fails on any template that reintroduces one of these.
- **`form-action` on a button that submits:** Chrome also checks the redirect that answers a form
  POST. If a form hands off to another site (OAuth, Stripe), `form-action` must name that site.
- **Workers:** `worker-src 'self' blob:`. `script-src` does not need `blob:`.
- **A tool injected into the page** (axe, a debugger): fix the page if that is reasonable. The
  Google Fonts stylesheets load with `crossorigin="anonymous"` so that axe can read them without
  fetching them again. Otherwise use `guard.allow(...)` in the spec with a pattern narrow enough
  that it cannot hide a real violation from the site.

## What still keeps `'unsafe-inline'` in `script-src`

Measured 2026-09-23: 126 inline `<script>` blocks in 99 templates, and 526 `on*=` handler
attributes in 160 templates. A nonce is not a gradual path: browsers ignore `'unsafe-inline'` as
soon as a nonce is present, so every block and every handler attribute would have to change in the
same release. P34 and P83 track moving that code into files.
