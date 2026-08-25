# UrbanLens — Goals

This is the authoritative, human-owned statement of product intent. It is intentionally short — a decision record, not a spec. Jess edits
this file directly; **agents must treat it as read-only** and never edit it. Where an
implementation, `README.md`, `docs/FEATURES.md`, or any other doc conflicts with a goal
stated here — or where intent isn't clearly established here — ask Jess rather than assume.

## Privacy model (non-negotiable, applies everywhere)

- A pin and everything inside it is private by default. There is no setting that makes a pin
  itself visible or searchable to another user. Full stop.
- The only way pin data reaches anyone else is an explicit, opt-in, consent-tracked copy —
  never a live reference:
  - **Pin → pin**: sender shares coordinates (+ optional bundled fields) as a *suggestion*;
    recipient must accept; on accept it becomes the recipient's own independent pin. The
    recipient never gets access to the sender's actual pin, before or after acceptance.
  - **Pin → wiki**: sender opts in per field; the wiki gets a copy. The wiki never has read
    access to the source pin or any data inside it.
- Every REST endpoint and every query must enforce this **by construction** — it must be
  structurally impossible for a bug (human or agent-written) to leak a private pin's live
  data through some other surface, not just conventionally discouraged.
- Litmus test for "are we referencing instead of copying": if editing a pin's fields 
  ever causes a *different* user's view (a trip activity, a game, a list) to change, that's a
  bug — that surface should be sourcing from the associated Wiki (or a copy), not the pin.
- Any shared surfaces, such as trips, or Games with multiplayer (SpotGuessr, Consensus, Trivia, etc.) source content
  from the associated Wiki, never directly from a pin. Single-user modes are the only possible
  exception — a user seeing their own pin data doesn't violate consent, since nothing is shared.
- Trip activities that reference a pin: treat trip-member sharing as explicit consent, tracked
  through the share-exposure system (`resolve_origin_share` / `record_share_exposure`). Display
  data should still come from the right place (the Wiki, or data consensually copied and stored separately), not a live pin reference.

## Wiki access

- Wikis are **not public**, despite the name. A user earns access to a location's wiki only by
  having their own pin inside that place's official boundary, potentially in addition to meeting other additional criteria — nothing else grants access.
- Purpose: users can only learn more about locations they already know exist. They must never
  be able to discover a location's existence through the wiki.
- Enforce by construction, everywhere: no endpoint may let a user search for, find, or see a
  wiki they haven't earned access to.
- Wiki data is versioned; the value shown for each field is resolved per-viewing-user.
  Users flagged as "concealed" (behavior patterns indicating data-mining rather than genuine
  community engagement) have certain fields hidden from them ("concealed data"), even on wikis
  they've otherwise earned. This must also hold by construction across every field, on every page.

## Encryption

- Direct messages: end-to-end encrypted by default, as close to unconditionally enforced as
  possible — there should be no way to turn it off. Optional self-destruct/disappearing
  messages; once self-destructed (read or timed out), the message — encrypted blob included —
  is gone from the server, not just hidden.
- Backups: strongly encrypted at rest, retained only as long as needed. Self-destructing
  messages are excluded from backups entirely, even encrypted — an unread message lost in a
  disaster-recovery restore is an acceptable tradeoff for the reduced attack surface.
- Logs — ours and any related service that stores IPs/access data — rotate and purge on a
  regular interval, not indefinitely.
- Trip photo archives (especially for past/completed trips) should be encrypted, ideally
  end-to-end.
- Longer-term direction, in roughly this order of "how close is this to feasible":
  1. Private photos and private notes: fully end-to-end encryptable today with no real
     tradeoff — we never need to search photo bytes (only EXIF/filename/AI tags), and notes are
     never sharable to begin with.
  2. Private pins: the main blocker is site search needing plaintext/queryable fields. A
     baseline worth building even if not final: decrypt-on-login into memory, re-encrypt and
     discard the key on logout/timeout. Aggregate cross-pin stats (e.g. "N users have this wiki
     pinned") and any "pins in common" style feature need to stop comparing pins/location
     models directly regardless — compare at the Place or Wiki level instead.
  3. Markup maps / shared photos: private and encrypted by default; sharing one (DM, wiki post)
     either duplicates it into an open copy, or is treated as the user electing to decrypt that
     one item — re-encrypt it if every share of it is later revoked.
  - Fully encrypting pin data would also structurally kill the recurring "can we just reference
    this pin's data from over here" confusion — worth weighing as a forcing function, not just
    a security upgrade.
- Add tests that actively try to defeat the encryption (property tests, brute-force-timing
  simulation, etc.), not just tests that assert the happy path round-trips.

## Games

- Purpose is twofold: fun, and sourcing facts-about-locations data (with confidence
  ranking) for wikis in a way some users will do that won't manually edit a wiki.
- Store gameplay-derived data in a schema general enough to support future/different analysis
  approaches, not just whatever ML pipeline is in use today.
- Respect the same privacy defaults as everything else; anonymize/forget the contributing user
  where the data's use case doesn't require attribution.

## Discovery: browse and search

- Both should be independently sufficient. A user should be able to find anything on the site
  by browsing alone (Memories, recents, saved filters, lists) with no search — and find
  anything by searching alone with no browsing. Audit for gaps against that bar.
- Every map on the site has the same controls (where they apply), layout, and behavior — no page should require
  re-learning the map. On specialized maps where unique controls apply, they should have a consistent behavior, layout, and style as other tools from other maps on the site.
- Saved filters apply near-instantly on the map — cached/indexed such that toggling one is not
  a perceptible round trip.
- Lists backed by a saved filter must reconcile automatic (filter-driven) membership against
  manual add/remove decisions: manual decisions persist even as filter membership changes, and
  a pin present via both the filter and a manual add is never shown twice. (Known gap: no UI
  today to see or undo the manual decisions made against a filter-backed list.)

## Safety check-ins

- Contacts do **not** get live location by default — only the trip plan, and only if the user
  fails to check in on time (an "incident"). A contact never needs to be a site member; access
  is via token link. The check-in owner can specify that a user does get live location and an ability to see the plan before a failed check-in, but this must be explicitly chosen and consent-focused.

## Public pins

- A private pin can become a "public pin" (visible without earned wiki access) only through a
  deliberately very strict, community-vote-driven bar. It's expected for this to rarely or never trigger in practice.
- A fresh/self-hosted deployment should have *some* way to avoid shipping with an empty map with no public pins — but the tooling used to seed new deployments shouldn't ship in the public release; other
  operators' deployments shouldn't be nudged to just copy our curated starter set. The seeding tool can be run once to seed production and then deleted (or moved to ../infrastructure)

## Self-hosting and federation

- UrbanLens is open source. A non-community deployment (no Kubernetes) must work fine without
  the mobile/community-scale infrastructure — a single-user desktop-style setup is a supported
  path, not an afterthought.
- Aspirational long-term direction: move toward a more distributed/federated/peer-to-peer
  architecture that minimizes what any single server operator can see. Okay if this is never
  fully reached — it should stay a standing direction, not a requirement to hit.

## Testing and CI/CD

- Every test asserts both the success path and the failure/negative path — a test that only
  ever checks for the expected success isn't proof the failure case is actually caught.
- Hypothesis property-based tests are additive to unit tests covering the same behavior, not a
  replacement for them. Redundant coverage via different strategies is a feature.
- Every page has (at an absolute minimum) one unit test and at least one integration test proving it loads with the expected content; every feature
  eventually gets the same coverage too.
- Investigate DB-collision-under-parallelism as a setup/teardown design problem first, before
  just scaling back parallelism — and treat Postgres falling over under test load as a possible
  signal of a real production performance issue, not purely test infra noise.
- Pre-commit and release-please friction should get fixed, not worked around.

## Ops / observability

- Recurring uptime-monitor alerts get investigated, not tuned out or normalized as noise.
- i18n/translation support is a near-term goal — the site is currently US/English-centric and
  the team is relocating to Europe.