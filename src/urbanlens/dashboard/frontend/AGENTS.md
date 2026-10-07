# dashboard/frontend/ — Frontend Cache Gotchas

Applies to `src/urbanlens/dashboard/frontend/`.

- `pin-cache.ts` has a `PIN_CACHE_VERSION` constant that must be bumped whenever the pin payload
  shape changes - it goes silently stale otherwise. The only writer is `pages/map/index.html`'s
  inline script, which spells the version and key out separately; `pin-cache.contract.test.ts`
  parses that template and fails if the two sides drift (they did once before).
- Non-map pages invalidate the map's pin cache via the `ul_pins_dirty` localStorage flag.