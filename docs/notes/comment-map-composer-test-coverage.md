# T3 — Nothing in the integration suite opens the comment-map composer; only a browser harness with a stubbed server does

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

`id: T3` · `status: open` · `updated: 2026-10-06` · `source: grep of tests/integration/specs and frontend/browser, 2026-10-06`

## The gap

```
$ grep -rliE "commentmap|attachmap|composer" tests/integration/specs
tests/integration/specs/ui/messages-photo-attachment.spec.ts
```

The one hit is the direct-message composer's photo chip, not the map composer. Nothing in
`tests/integration/` opens the map composer (`#comment-map-composer`,
`templates/dashboard/partials/map/_markup_composer_dialog.html`) against a real server, saves a
map through it, or checks that the saved map round-trips into a comment or a message.

## What does cover it now

`src/urbanlens/dashboard/frontend/browser/markup-composer.test.ts` (`bun run test:browser`) drives
the composer in Chromium: the built `core.js`, the real `comment-map.js`, and the real dialog
partial spliced into a harness page (`composer-harness.html`). It draws, selects, deletes, edits
geometry and style, uses the Layers list, turns the map, saves, reopens a legacy map, and checks the
download's pixels and burned-in credit. It has no Django and no database: the save endpoints are
answered by the test's own `Bun.serve`, which records the posted body, and tiles are solid-colour
GIFs. So it establishes that the client builds the right snapshot, not that the server stores it.

The server half is covered separately in pytest
(`dashboard/tests/hypothesis/test_markup_map.py`, `test_markup_map_rotation.py`): the create,
view-state and snapshot endpoints, sanitising, and the model round trip.

`dashboard/frontend/ts/shared/markup-composer.test.ts` (happy-dom, `bun run test:ts`) checks that
every element id `markup-composer.ts` and `comment-map.js` look up exists in the partial.

## What would close this

A spec (`tests/integration/specs/ui/comment-map.spec.ts` or similar) against the integration
stack that:

- opens the composer from a surface that has one (a comment box, the messages composer, or the main
  map's "take a screenshot" action);
- draws something and saves it;
- asserts the saved map appears where it should (rendered in the comment or message, or through
  the API), including a turned map reopening turned;
- runs signed in **and** signed out, per the theme's own signed-out handling
  (`test_the_config_survives_a_signed_out_request` in
  `dashboard/tests/hypothesis/test_shared_theme_script_is_cacheable.py` covers the config half).

## What this does not establish

That the composer works against the deployed CSP (`static.arcgis.com` in `connect-src`, unpkg for
leaflet-rotate in `script-src`) or with real tiles from this deployment's proxy. The harness serves
neither.
