# T3 — Nothing in the integration suite opens the comment-map composer; a page-error guard is the only thing standing behind it

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

`id: T3` · `status: open` · `updated: 2026-09-16` · `source: grep of tests/integration/specs, 2026-09-16`

## The gap

```
$ grep -rliE "commentmap|attachmap|composer" tests/integration/specs
$ grep -rliE "commentmap|attachmap|composer" tests/integration/specs | wc -l
0
```

No spec file references `CommentMap`, `markup`, `attachMap` or `composer`. Nothing in
`tests/integration/` opens the comment-map/image-attachment composer, submits it, or checks that a
map it saves round-trips.

## Why it matters more after X21

X21 (`docs/notes/inline-script-extraction-map-and-theme.md`) moved this composer's code out of
`themes/base.html` and into `frontend/static/js/comment-map.js`, loaded by every page that extends
the base theme - which is every page in the app. The only thing standing behind that file today is
`tests/integration/specs/smoke/pages.spec.ts` plus the global page-error guard
(`tests/integration/lib/page-guard.ts`, wired to `page.on("pageerror", ...)`): if the file throws
on load, smoke fails the page. Nothing asserts the composer *works* - that it opens, that a user can
draw or place a marker, that "save" persists anything, or that a saved map round-trips into a
comment or a message.

X21 also documents a defect this exact failure mode would have caught immediately had the spec
existed: the `const CFG` collision between `map-page.js` and `comment-map.js` was found by the
existing UI suite (`map.spec.ts`) failing on an unrelated assertion after the page's own script
died, not by anything that opened the composer itself. A composer-specific spec would have named
the actual break instead of a symptom of it.

## What would close this

A spec (`tests/integration/specs/ui/comment-map.spec.ts` or similar) that:

- opens the composer from a surface that has one (a comment box or the messages composer - grep
  `attachMap`/`CommentMap` call sites in `frontend/static/js/comment-map.js` and its remaining
  template wiring for which surfaces those are, since neither is exercised by any existing spec);
- places or draws something and saves it;
- asserts the saved artifact appears where it should (rendered in the comment/message, or via the
  API);
- runs signed in **and** signed out, per the theme's own signed-out handling
  (`test_the_config_survives_a_signed_out_request` in
  `dashboard/tests/hypothesis/test_shared_theme_script_is_cacheable.py` covers the config-rendering
  half of this in pytest; nothing covers the browser half).

## What this does not establish

Whether the composer currently works end to end in a browser - X21's commit message asserts the
`CFG`-collision fix was "confirmed" by the browser suite, but that suite does not open the composer,
so that confirmation is about the map page's own script, not this one. Not verified either way this
session.
