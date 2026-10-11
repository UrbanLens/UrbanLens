---
status: accepted
date: 2026-09-15
---

# The map cache is an accelerator the site can lose, and labels ship once per document

Formerly `D12`.

Jess's constraint is that not every user's data can stay cached forever, and that prewarming may only make an already fast site snappier. So the map must be correct and fast enough with the cache empty. The mutable per-pin cache, which had three known races, was replaced by an immutable, version-keyed document cache (`SET NX` on a key derived from the content's ETag), with nothing a race can corrupt. Pins carry `label_ids`, and each label ships once in a per-document dictionary instead of being copied into every pin.

## Considered options

- Vector tiles for the per-user map: rejected, because the map is a client-side data model (popups, list, instant filters), and tiles would turn "load once, pan freely" into a request per pan. They suit the unbounded public map instead.

## Consequences

- The streamed NDJSON document and derived ETag are built. The `?since=` delta, viewport mode above the pin ceiling, and prewarm-on-write are deferred, each with a stated trigger. The filter POST returning data (Decision 4) is deferred at Jess's request.
- A label edit still invalidates the cached document, because it moves the pins' fingerprint.
