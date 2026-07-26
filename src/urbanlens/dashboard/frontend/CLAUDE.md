# dashboard/frontend/ — Frontend Cache Gotchas

Applies to `src/urbanlens/dashboard/frontend/`.

- `pin-cache.ts` has a `CACHE_VERSION` constant that must be bumped whenever the pin payload
  shape changes - it goes silently stale otherwise.
- Non-map pages invalidate the map's pin cache via the `ul_pins_dirty` localStorage flag.