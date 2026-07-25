# Public Pins by Community Vote (UL-58)

Status: DRAFT — rules normalized from product notes 2026-07-23; implementation mapping below.

## Goal

A tiny, highly selective set of locations can be voted "public" by the community. Public
locations are suggested to all users (opt-out), giving new accounts a populated map without
exposing anything vulnerable. Selectivity is the point: the bar is deliberately high and the
rules are enforced server-side. Users never see the rule engine — they only see vote buttons
in the rating section when a location qualifies.

## Vocabulary

- **Location** — the shared wiki-backed place record.
- **Candidate** — a Location currently eligible for a public vote (system-computed, never
  user-initiated).
- **Public location** — a Location the community voted public. Immutable outcome (only an
  admin/data migration can revert).

## Eligibility (ALL must hold; recomputed continuously)

A location is eligible iff every criterion below passes. Eligibility is computed by a
periodic task, never inline in a request.

1. **Region exclusivity** — no already-public location within `REGION_RADIUS_KM` (default
   **15 km**, roughly a city). One public pin per city-sized region, ever.
2. **Low vulnerability** — the wiki's vulnerability stat has **≥ 3 votes** and its average
   is **< 2** on the wiki's existing vulnerability scale (lower = less vulnerable).
3. **Wiki completeness** — all of:
   - a *meaningful name*: not blank, not a bare coordinate/placeholder pattern, ≥ 4 chars
     after stripping, and not equal to an auto-generated default;
   - **≥ 1 additional alias** beyond the name;
   - **≥ 2 photos**;
   - **≥ 2 links**;
   - a non-trivial **article** (stripped length ≥ `MIN_ARTICLE_CHARS`, default 280);
   - **≥ 1 map markup element or child pin** on the wiki map.
4. **Popularity band** — the number of distinct users with a pin at the location is
   **≥ max(2, min(10, ceil(20% × active users)))**. ("Active users" = accounts that logged
   in within the last 180 days; constants tunable in one config block.)
5. **Top-10 in state** — among locations passing criteria 1–4, it ranks in the **10 most
   commonly pinned in its US state** (ties at the cutoff all qualify). Locations without a
   resolvable state are ineligible.

Rationale for the order: 1–4 are per-location predicates; 5 is a rank over the survivors,
matching "calculate 10 most common after the criteria above are filtered out".

## Vote lifecycle (state machine on `PublicPinCandidate`)

```
             criteria met                    criteria lapse
  (none) ──────────────────▶ OPEN ◀────────────────────────▶ SUSPENDED
                              │ pass conditions met
                              ▼
                           PASSED  (location becomes public; terminal)
                              │
  OPEN ── hard-fail ──▶ REJECTED (terminal; location permanently ineligible)
```

- **OPEN**: created the first time criteria pass; `opened_at` set once and never reset by
  suspension (the 1-week clock measures total time since opening, not continuous
  eligibility — suspension is expected to be rare and brief, and resetting the clock would
  let a flapping criterion filibuster the vote).
- **SUSPENDED**: criteria lapsed. Votes are frozen and retained; buttons disappear from the
  UI; pass/fail conditions are not evaluated. Returns to OPEN automatically if criteria
  pass again.
- **PASSED** requires ALL of: state is OPEN at evaluation time; `now − opened_at ≥ 7 days`;
  total votes ≥ 2; yes-share ≥ 75%.
- **REJECTED** (hard fail) at ANY evaluation: total votes ≥ 10 and no-share ≥ 75%. The
  location is marked permanently ineligible (`public_vote_rejected` on the candidate row —
  the eligibility engine skips it forever).
- Otherwise the vote stays open indefinitely.

### Who votes, and how

- Only users **with a pin at the location** may vote (they are the only ones who can see
  the rating section anyway; enforced server-side too).
- One vote per user per candidate; users may **change or withdraw** their vote at any time
  while the candidate is OPEN.
- Votes are anonymous in the UI — only the voter sees their own choice; no counts are
  shown until the outcome (avoids bandwagons and keeps the surface simple).

### What happens on PASSED

- `Location.made_public_at` set; the location joins the public set.
- All existing per-user pins are untouched.
- Users **without** a pin there start receiving it as a **pin suggestion** (existing
  suggestion surface), unless they've turned off "Suggest community-approved public
  locations" in settings (new toggle, default ON).
- Public locations never expose who pinned them, who voted, or any per-user data — the
  suggestion carries only the wiki (community) view.

## UX (deliberately minimal — rules are never explained inline)

- **Rating section, candidate OPEN, user hasn't voted**: a small block —
  “Should this location be visible to every UrbanLens user?” with two buttons:
  **Make it public** / **Keep it private**, plus a “What's this?” link → FAQ anchor.
- **User has voted**: “You voted to make this location public/private.” with a
  **Change vote** affordance (swaps back to the two buttons) and **Withdraw**.
- **SUSPENDED / ineligible / REJECTED**: nothing renders. No explanation, no teaser.
- **PASSED**: the block is replaced by a quiet “Public location” badge in the wiki header.
- **Suggestions**: public locations flow through the existing suggestion surface with an
  “approved by the community” one-liner; dismiss and accept behave like any suggestion.
- **Settings**: one checkbox under privacy/notifications-adjacent group:
  “Suggest community-approved public locations” (default on).
- **FAQ**: one entry ("Why are so few locations public?") explaining, in plain language:
  public status is community-voted, restricted to well-documented, low-risk, widely-known
  places, at most one per area, and that the bar is intentionally high to protect
  locations — without enumerating thresholds.

## Mechanics & performance

- **Eligibility engine**: hourly Celery beat task `evaluate_public_pin_candidates`.
  Pipeline: (a) SQL-annotate per-location aggregates (pin-count, vulnerability count/avg,
  photo/link/alias/markup/child counts) over locations with any pins, excluding rejected
  and already-public; (b) filter criteria 1–4 in the DB where possible; (c) rank per state
  (window function) for criterion 5; (d) diff against existing candidates → open / suspend
  / reopen; (e) evaluate pass/fail for OPEN candidates.
- **On-vote fast path**: casting a vote re-evaluates ONLY the hard-fail condition (cheap
  count) — never the pass condition, which stays on the periodic cadence so the 1-week
  floor can't be gamed by vote-time races. (Hard-fail on the write path keeps a
  brigaded-No vote from lingering visible for up to an hour.)
- All thresholds live in one dataclass (`PublicPinConfig`) so tuning is a one-file change.
- Everything the UI needs (candidate state + the viewer's own vote) is fetched in the
  existing rating-section query path — one extra `select_related`/`prefetch`, no N+1.

## Config defaults (tunable)

| Constant | Default | Note |
|---|---|---|
| `REGION_RADIUS_KM` | 15 | "about the size of a city" |
| `MIN_VULN_VOTES` | 3 | |
| `MAX_VULN_AVG` | 2.0 (exclusive) | |
| `MIN_ALIASES` | 1 (beyond name) | |
| `MIN_PHOTOS` | 2 | |
| `MIN_LINKS` | 2 | |
| `MIN_ARTICLE_CHARS` | 280 | "meaningful article" |
| `MIN_MARKUP_OR_CHILDREN` | 1 | markup elements + child pins ≥ 1 |
| `TOP_N_PER_STATE` | 10 | ties at cutoff included |
| `PINNED_BY_FLOOR` | max(2, min(10, ceil(0.20 × active users))) | active = 180-day login |
| `MIN_VOTES_TO_PASS` | 2 | |
| `MIN_OPEN_DAYS` | 7 | |
| `PASS_CONSENSUS` | 0.75 | yes / total |
| `FAIL_MIN_VOTES` | 10 | |
| `FAIL_CONSENSUS` | 0.75 | no / total |

## Implementation mapping (built 2026-07-23)

- Models: `models/public_pins/` — `PublicPinCandidate` (OneToOne→Location,
  `related_name="public_candidate"`; status open/suspended/passed/rejected; opened_at /
  decided_at) and `PublicPinVote` (candidate+profile unique; withdraw = row delete,
  mirroring WikiStatVote). Migration `0015_public_pin_voting` (also adds
  `Profile.suggest_public_pins` and the `community` PinSuggestionOrigin).
- Engine: `services/public_pins.py` — `PublicPinConfig` (all tunables), `evaluate_public_pin_candidates`
  (per-aspect aggregate queries: distinct Counts annotation + grouped vulnerability
  composite + `Length(content)` on Article; haversine for region exclusivity; per-state
  top-N with ties). Vulnerability = `WikiStatVote` VULNERABILITY composite (1–5 scale).
  "Markup or child pin" = `wiki.markup_items` + `wiki.child_wikis`.
- Beat: `tasks.evaluate_public_pin_candidates`, hourly (`CELERY_BEAT_SCHEDULE`
  "public-pin-candidate-evaluation").
- Endpoint: `PublicPinVoteView` — POST `/location/<slug>/wiki/public-vote/`
  (`location.wiki.public_vote`), choice=public|private|withdraw, re-renders the block.
- UI: `partials/pins/_public_pin_vote_block.html` under the Community Ratings grid in
  `pages/location/wiki.html`; styles in `_wiki.scss` (`.public-pin-vote`).
- Suggestions: `sync_public_pin_suggestions` (idempotent fan-out, origin=community,
  respects `community_enabled` + `suggest_public_pins`); queue filter in
  `controllers/pin_suggestions._pending_suggestions` hides (not deletes) community
  suggestions while the toggle is off.
- Setting: Community section of Settings (`CommunitySettingsForm.suggest_public_pins`).
- FAQ: "What's a public location, and why are there so few?" (`#public-locations`,
  Privacy & sharing section).
- Tests: `tests/hypothesis/test_public_pins.py` (rule helpers, engine criteria matrix,
  lifecycle/settlement, endpoint, suggestion sync).
