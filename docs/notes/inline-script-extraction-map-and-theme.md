# X21 — Moving two inline scripts to cached files cut what the map page re-sends by 275 KB, and a collision between them briefly broke the map page until each file got its own config name

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

`id: X21` · `type: experiment` · `status: holds` · `updated: 2026-09-16` · `source: commits 23a861765 and 3c924327e on release/v_0_8_0; src/urbanlens/core/tests/inline_scripts.py; verification commands below, run against urbanlens_development_main, session of 2026-09-16 (ad hoc, not preserved as a script)`

## Question

`docs/PROBLEMS.md` P34 and P83 named the map page's inline `<script>` (5,175 template lines,
measured 2026-08-14) and `themes/base.html`'s comment-map composer (1,118 lines, same date) as two
of the biggest untested, uncacheable blocks in the app. Commits `23a861765` and `3c924327e` moved
both to static files. This records what that move actually changed, on disk and in a browser, and
the two defects the mechanical part of it introduced.

## What moved

Sizes of the `<script>` content at the commit immediately before each move
(`git show <parent> --numstat -- <template>`, `git show <parent>:<template>` piped through a
`<script>` regex):

| | template line before | bytes before | now |
|---|---|---|---|
| map page's program | `pages/map/index.html`, 5,389 lines removed | 275,276 | `frontend/static/js/map-page.js`, 275,049 bytes |
| comment-map/image-attachment composer | `themes/base.html`, 957 lines removed | 51,986 | `frontend/static/js/comment-map.js`, 52,134 bytes |

The map page's block was inside a ~589 KB authenticated response and re-sent, uncached and
unminifiable, on every visit. The composer was inside `themes/base.html`, which every page extends,
so every page paid for it whether or not it ever opened a composer.

The per-request values that used to be interpolated into the template travel as `json_script`
elements instead: `#map-page-config` (52 entries, built by `services/map_pins/page_config.py` from
context the view already renders) and `#comment-map-config` (8 entries, built by a `@deferred`
context processor, `add_comment_map_config`, so a page that never renders the composer builds and
sends nothing).

**Verified serving, 2026-09-16**, against `urbanlens_development_main` (`docker exec` into the app
container found the hashed names in `frontend/static/staticfiles.json`):

```
$ curl -sD - -o /dev/null http://localhost:21810/static/js/map-page.096ab74ec4c2.js
Cache-Control: public, max-age=31536000, immutable
Content-Encoding: gzip
Vary: Accept-Encoding
ETag: W/"6aa9e2f5-43269"

$ curl -s -o /dev/null -w '%{http_code}\n' -H "If-None-Match: \"6aa9e2f5-43269\"" \
    http://localhost:21810/static/js/map-page.096ab74ec4c2.js
304
```

**The gzip figure quoted in both commit messages (69 KB / 13 KB) is not what nginx actually sends.**
`src/urbanlens/config/nginx/nginx.conf:109` turns gzip on and never sets `gzip_comp_level`, so it
defaults to level 1 (already established by X20) - confirmed here by pulling the compressed bytes
off the wire without letting curl decode them:

```
$ curl -s -H "Accept-Encoding: gzip" -o /tmp/m.gz http://localhost:21810/static/js/map-page.096ab74ec4c2.js -w '%{size_download}\n'
84955
$ curl -s -H "Accept-Encoding: gzip" -o /tmp/c.gz http://localhost:21810/static/js/comment-map.5d8849a6a486.js -w '%{size_download}\n'
15788
```

84,955 and 15,788 bytes (level 1) are what actually crosses the wire on a first visit, against
68,733 and 13,000 at level 9 - the number worth quoting if this ever gets cited as a bandwidth
saving. Either way, the caching win does not depend on the compression level: `max-age=31536000,
immutable` means a returning visitor's browser does not even issue the conditional request above:
the file is fetched once and never again for a year, where the inline version was re-sent in full
on every navigation regardless of compression.

## Two defects the mechanical move introduced

**(a) A template tag inside a quoted string is not substitutable in place.**
`'ul_addr_history_v1_{{ request.user.profile.id }}'`, copied byte-for-byte out of the template,
becomes the literal text `CFG.profileId` once the Django tag is gone and nothing replaces it with a
concatenation. Four sites hit this: two were `localStorage` keys (address history and recent pins -
`ul_addr_history_v1_` / `ul_recent_pins_v1_`), which would have keyed every account's browser
storage to one shared literal string, so on a shared browser one signed-in user would see another's
saved history; two were `fetch()` URLs, which would merely have 404'd. Fixed as string
concatenation, e.g. `'ul_addr_history_v1_' + MAP_CFG.profileId + ''`
(`frontend/static/js/map-page.js:4288-4289`). Now enforced generally: `inert_reads()`
(`src/urbanlens/core/tests/inline_scripts.py:109-130`) walks every `CFG.x` read in a script, finds
the quote delimiter open at that position, and fails if there is one - a config read is either live
code or it is nothing.

**(b) Both generated files declared `const CFG` at top level.** The map page loads both
`map-page.js` and `comment-map.js` (every page can open the comment-map composer, including the
map page). The second `const CFG` declaration is a `SyntaxError: Identifier 'CFG' has already been
declared`, thrown before either script runs a line - it stopped the map's entire program, not just
the composer. Nothing in Django's test suite could see this: each extraction was verified
one file at a time, and a single file with `const CFG` is syntactically fine on its own. The
Playwright suite caught it only once both files coexisted on the same page. Fixed by naming each
file's config after itself - `MAP_CFG` (`map-page.js:3`), `COMMENT_MAP_CFG` (`comment-map.js:3`).
**The lesson worth keeping:** a check that each generated file is individually correct proves
nothing about two of them sharing one global scope; that needs a check (or a browser) that loads
them together.

## Verification run this session

`bin/run_tests.sh` (not `docker exec <app> pytest` - see `docs/notes/database-roles.md`, R29: the
app container's `ul_web` role cannot create a test database after the per-tier role rollout):

```
$ UL_TEST_DB_NAME=doc_verify_run_1789519745 bin/run_tests.sh \
    src/urbanlens/dashboard/tests/hypothesis/test_map_page_script_is_cacheable.py \
    src/urbanlens/dashboard/tests/hypothesis/test_shared_theme_script_is_cacheable.py -q
14 passed in 235.37s
```

Those 14 cover: no inline block over `MAX_INLINE_SCRIPT_BYTES` remains, the map program specifically
is never inline, each page loads its script from `js/`, the config element carries everything the
script reads (`missing_config`), no Django template syntax survives into the `.js` file
(`template_syntax`), no config read is inert (`inert_reads`), and - for the theme script - that a
signed-out visitor (`request.user.profile` does not exist) still gets a valid config rather than a
500 from the context processor.

**Not re-run this session, cited from the commits:** Playwright `tests/integration/specs/ui/map.spec.ts`
(5/5 - Leaflet initialises, pin feed loads, pin-list panel opens, the HTMX filter round-trips,
basemap catalogue) and a full smoke+ui run showing zero page errors after the `CFG` fix. Re-run
before relying on this if the map page's JS changes again.

## What this does not establish

- The other three of P34's original top-5 templates (`messages/index.html`, `trips/detail.html`,
  `location/index.html`) are untouched - see P34's rewrite.
- `themes/base.html` still carries 242 lines of other inline script (an `html-root` IIFE, a hotkeys
  JSON block, passwordless-account wiring) - not this session's target, not measured for defects.
- Pin-detail's own largest inline block (59,463 bytes, unrelated to either extraction) and Settings'
  (28,694 bytes) are untouched - see P83's rewrite.
- Neither file is a `tsc`-checked bundle; both are hand-written `.js` served statically. P92's
  cluster-badge duplication (`map-page.js:343-364` vs `shared/map-clusters.ts`) is exactly as
  unresolved as before the move, just re-cited to the new path.
- Whether other templates hide the same two defect shapes (a tag inside a string; a global name
  collision) was not swept for; only these two extractions were checked.
