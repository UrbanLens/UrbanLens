---
status: accepted
date: 2026-08-27
---

# Thirteen rulings on the mobile app team's API asks

Formerly `D5`.

The mobile team's requirements doc asked for API features that existed in some form or not at all. The note records which form each one took, with its reasoning, under local labels D1–D13 in its Part 3; those labels are not global decision ids. Only P0 and P1 asks were built in that pass. Every P2 ask is deferred, not declined, unless the note says otherwise.

## Consequences

- E2EE key endpoints are published in the external schema, not duplicated under `/api/external/v1/`: two copies of a key-exchange contract can drift until messages stop decrypting.
- Declined: pin-note threading and reactions (use pin comments) and wiki detail-pin / child-wiki CRUD.
- Game scopes are `games:read`/`games:write`, not `pins:*` and not a single `games:play`.
- Panel imagery is never inlined as base64 in JSON. Browser-origin third-party clients are out of scope for CORS in v1.
- Per-conversation disappearing messages are blocked on a product decision: whose setting it is, and whether it is retroactive.
