# SpotGuessr (UL-391..UL-396)

Status: DRAFT — rules normalized from product notes 2026-07-24. Phase 1 (UL-391) is being
built now; UL-392..UL-396 are follow-up tickets, specified here so the data model doesn't
need to be re-shaped when they land.

## Goal

A GeoGuessr-style game built on UrbanLens's own pin/wiki/photo data: players guess where a
photo was taken, or where a named place is, using only locations every participant already
knows (has pinned). Skill and difficulty are tracked with Glicko-2 so both players and
locations converge to a meaningful rating over time.

## Vocabulary

- **Location** — the shared, immutable coordinate row (existing model).
- **Pin** — a profile's personal claim on a Location (existing model). "Pinned by everyone
  in the session" is the core eligibility gate for every mode.
- **GameSession** — one playthrough: a mode, a config, a fixed number of rounds, one or more
  participants.
- **GameRound** — one location to guess, within a session.
- **Guess** — one participant's answer to one round.
- **Player rating** — a profile's Glicko-2 skill rating, tracked per mode (a Photos-mode
  rating is independent of a Street View-mode rating).
- **Location rating** — a location's Glicko-2 *difficulty* rating, also tracked per mode
  (the same location can be "easy" as a Photos round and "hard" as a Street View round).

## Eligibility (ALL must hold; recomputed per round, never cached across sessions)

A Location is eligible for a round in session S iff:

1. **Pinned by every participant.** A `Pin` row exists for `(participant, location)` for
   every profile in the session — including solo sessions (trivially, the one player's own
   pins). This is the one rule the product spec repeats verbatim for every mode; it is
   enforced in `services.spotguessr.eligibility`, never left to the caller.
2. **Visited by everyone** (optional, default OFF, `config.require_visited_all`) — every
   participant additionally has a `PinVisit` row against their pin at that location.
3. **Inside the configured geographic boundary** (optional, `config.geo_bounds`, a GeoJSON
   polygon/bbox) — the location's `point` falls inside it.
4. **Not already used in this session** — GeoGuessr-style, no repeats within one
   playthrough.
5. **Mode-specific data exists** — Photos mode additionally requires at least one eligible
   `Image` (see Photo selection below); Named Place mode requires a non-blank wiki name (not
   built in Phase 1); Street View mode requires the Street View API to have imagery (not
   built in Phase 1).

## Scoring: point vs. boundary distance

The product rule, restated precisely: **measure distance from whatever is actually specific
about the guess target — never from an arbitrary stand-in coordinate.**

- A photo with its **own** GPS coordinates (EXIF or user drag-placement — `Image.latitude`/
  `longitude`) represents a *point*. Score = geodesic distance from the guess to that exact
  point.
- A photo with **no** coordinates of its own represents *the location*, not a point. Score =
  distance from the guess to the location's **effective property boundary**
  (`Boundary.effective_polygon`, which already falls back to a generated circle when no real
  polygon exists) — **0 if the guess lands inside it.**
- Named Place mode (UL-393) always scores against the boundary, never a point, even though a
  Location has a single `point` — a place is an area, not a dot, and the whole reason this
  mode exists is to reward "know the neighborhood" over "know the exact marker."
- Street View mode (UL-393) scores like a coordinate-bearing photo: the capture point is
  specific, so point-distance applies.

This is resolved once per round by `services.spotguessr.scoring.resolve_target()` and stored
on `GameRound.target_is_point` / `target_point` at round-creation time (a snapshot, since a
photo's own coordinates could theoretically be corrected later — the round should stay
consistent with what the player actually saw). Boundary-based rounds are **not** snapshotted;
they resolve against the location's *current* boundary at guess time, since boundaries are
community-maintained and get more accurate over time — re-litigating an old round's boundary
would be strictly worse than using the best data available now.

Distance is computed geodesically in the database (PostGIS `Distance()` over a `geography`
cast), matching the existing convention in `models/pin/queryset.py` and
`services/memories/photos.py` — never the codebase's other, approximate
"degrees × 111,320" shortcut (`services/map_sharing.py`), because scoring fairness depends on
it being right at small (sub-km) scales, not just roughly right at trip-planning scale.

## Points

```
location_points = round(MAX_ROUND_POINTS * exp(-distance_km / DISTANCE_DECAY_KM))   # floor 0
date_points     = round(MAX_DATE_POINTS  * exp(-abs(days_off) / DATE_DECAY_DAYS))    # floor 0, only when date guessing is on
```

Exponential decay (not linear) so precision near the target matters far more than precision
far away — a guess 50m off and a guess 200m off should feel meaningfully different; a guess
20km off and a guess 40km off should both just read as "wrong." `location_points` is what
feeds the Glicko-2 rating update (see below); `date_points` is purely a side-score, never
mixed into skill rating — guessing well from a photo and guessing well from EXIF-adjacent
reasoning ("this car model dates the photo") are different skills, and conflating them would
make the core rating noisier for players who never enable date guessing.

## Glicko-2 ratings: player skill vs. location difficulty

Two independent rating pools per mode, both plain Glicko-2 (Glickman, "Example of the Glicko-2
system", 2012) with the standard defaults (rating 1500, RD 350, volatility 0.06, scale factor
173.7178, system constant τ=0.5):

- `PlayerModeRating(profile, mode)` — the player's skill.
- `LocationModeRating(location, mode)` — the location's difficulty.

Each **round** is treated as one Glicko-2 rating period for both sides:

- The player's rating updates once, with the location as its sole "opponent," using outcome
  score `s = location_points / MAX_ROUND_POINTS` (clamped to [0,1]) — a perfect guess is a
  "win," a hopeless guess is a "loss," anything between is a fractional result exactly the
  way Glicko-2 already supports (it's designed for game outcomes in [0,1], not just 0/0.5/1).
- The location's rating updates using every participant in that round as opponents, with
  outcome score `1 - s` per participant — a location that nobody can find is "winning" against
  the field, which is exactly the "high difficulty rating" a hard location should earn.

This is a deliberate repurposing of a two-player rating system as a symmetric skill↔difficulty
pairing, not a novel algorithm — the point is that plain, well-tested Glicko-2 math applies
unmodified on both sides; only the meaning of "opponent" and "score" is chosen to fit this
game. Both rating rows expose the standard Glicko-2 outputs on the *display* scale
(`rating = 1500 + 173.7178 × μ`, `rating_deviation = 173.7178 × φ`) so the UI never has to
know the internal-scale constants.

## Difficulty slider

`config.difficulty` is a 0.0 (easiest) – 1.0 (hardest) float. It maps to a target display
rating via `MIN_LOCATION_RATING + difficulty × (MAX_LOCATION_RATING − MIN_LOCATION_RATING)`,
then candidate locations are weighted by a Gaussian kernel on
`|location_rating.rating − target_rating|` (bandwidth `DIFFICULTY_BANDWIDTH`). Locations with
fewer than `MIN_GAMES_FOR_DIFFICULTY_WEIGHTING` rounds played keep the neutral default rating
(1500) rather than being penalized for lacking history — a brand-new location is never
excluded just because nobody has rated it yet.

## "Feels random" selection (anti-clustering)

Uniform-random selection over a small, geographically clustered pin set reliably produces
"two guesses from the same block in a row," which reads as broken rather than random. Instead:

1. Build the eligible-location pool (see Eligibility).
2. Exclude every location already used in this session (hard rule, never relaxed).
3. Exclude locations within `MIN_SEPARATION_KM` of the **immediately preceding** round's
   location. If this empties the pool, relax this one constraint only (never rule 2).
4. Weight the remaining pool by the difficulty-slider kernel above.
5. Weighted-random pick (`random.choices`).

## Photo selection (Photos mode)

`services.spotguessr.photos.candidate_image_for_location()` — for Phase 1, pulls from
`Image` rows already on the location (`Image.location_id = location.id`, `media_type=photo`).
There is deliberately **no separate "opted into the game" gate in Phase 1**: for a solo
session this raises no privacy question (it's the player's own pins/photos, or public wiki
photos on locations they've pinned), and gating on a not-yet-built community-submission flag
would just mean Photos mode has nothing to show. The community submission/consent pipeline
(UL-394) becomes the actual photo source once multiplayer (UL-392) ships, since that's where
showing a stranger's private photo without consent would actually matter — see UL-394 below.

`config.external_media_only`, if set, restricts to `Image.source != upload` (uses the
existing `ImageSource` enum — everything that isn't a plain personal upload already reads as
"externally sourced": Wikimedia, Google Images, Smithsonian, etc.).

## Solo vs. multiplayer (Phase 1 = solo only)

`GameSession` and `GameSessionParticipant` are modeled as a proper many-participant session
from the start (not a "solo-only" shape retrofitted later) because every eligibility/scoring
rule already reads "all participants," not "the player." Phase 1 only ever creates
single-participant sessions; UL-392 adds the invite/join/real-time-sync flow on top of the
same tables.

## Config defaults (tunable — one dataclass, `SpotGuessrConfig`)

| Constant | Default | Note |
|---|---|---|
| `MAX_ROUND_POINTS` | 5000 | GeoGuessr-familiar scale |
| `DISTANCE_DECAY_KM` | 2.0 | tuned for metro-area pin density, not GeoGuessr's world scale |
| `MAX_DATE_POINTS` | 1000 | secondary to location score |
| `DATE_DECAY_DAYS` | 180 | half a year |
| `DEFAULT_ROUNDS_PER_SESSION` | 5 | |
| `MIN_ROUNDS_PER_SESSION` / `MAX_ROUNDS_PER_SESSION` | 3 / 20 | |
| `MIN_LOCATION_RATING` / `MAX_LOCATION_RATING` | 1000 / 2000 | difficulty-slider target band |
| `DIFFICULTY_BANDWIDTH` | 200 | Gaussian kernel width, in rating points |
| `MIN_GAMES_FOR_DIFFICULTY_WEIGHTING` | 5 | below this, treat as neutral (1500) |
| `MIN_SEPARATION_KM` | 0.5 | anti-clustering exclusion radius from the previous round |
| Glicko-2: rating / RD / volatility / scale / τ | 1500 / 350 / 0.06 / 173.7178 / 0.5 | Glickman's published defaults |

## Social: ratings visibility

- A profile always sees its own `PlayerModeRating` rows.
- A friend's rating is visible on the SpotGuessr overview page only if that friend's
  `SpotGuessrPreference.show_ratings_to_friends` is `True` (**default True** — opt-out, per
  spec). Enforced server-side in the view, mirroring every other `*_visibility` gate on
  `Profile` rather than being a client-side hide.
- `SpotGuessrPreference` is a dedicated `OneToOneField(Profile)` model (same shape as
  `NotificationPreference`/`SafetyPreference`), not new columns bolted onto `Profile` — it
  also holds `last_config` (a JSON blob of the player's last-used game settings, mirroring
  `Profile.home_widget_layout`'s "remember my preferences" role) so returning to the game
  doesn't reset the difficulty slider/toggles every time.

## Phase mapping

- **UL-391 (this pass)** — data model (ratings, session, round, guess, preference);
  Glicko-2 engine; eligibility engine; point/boundary distance scoring; date-guessing bonus;
  difficulty slider; geographic boundary filter; anti-clustering selection; solo-only Photos
  mode end to end (start session → round → guess → reveal → summary); own-rating + friends'
  -ratings-with-opt-out display. Models: `models/spotguessr/`. Services:
  `services/spotguessr/`. Controller: `controllers/spotguessr.py`. UI: Leaflet click-to-guess
  map + pin-search, `frontend/ts/entries/spotguessr.ts`.
- **UL-392 (follow-up)** — multiplayer sessions: invite/join flow, a `GameSessionConsumer`
  (Channels, mirroring `DirectMessageConsumer`'s group-per-session pattern) for round
  sync/live scoreboard, and live text chat scoped to the session.
- **UL-393 (follow-up)** — Named Place mode (boundary-distance guessing from a name/alias,
  no search, with a setting to disable aliases) and Street View mode.
- **UL-394 (follow-up)** — community photo submission pipeline: upload-to-wiki with a
  submit-to-game checkbox (and an upload notice that it was added to the location's wiki),
  a "submit this wiki photo to the game" button in the lightbox, report/flag buttons (photo
  isn't of a location; revealed location is wrong for this photo), thumbs up/down, and the
  moderation classifier — reusing the existing Cloudflare Workers AI vision gateway
  (`services/ai/vision.py`'s provider/rate-limit pattern) with a person/nudity-capable model.
  Per spec: a photo that fails the classifier is simply never used, silently — the submitter
  gets no signal either way, so failing the check carries no feedback loop to route around it.
- **UL-395 (follow-up)** — voice chat: peer-to-peer WebRTC mesh, signaling relayed over a new
  Channels consumer (no SFU/new infra — chosen because sessions are small, and it avoids a
  new paid dependency). Suits 2-6 participants; would need revisiting if session sizes grow.
- **UL-396 (follow-up)** — engagement polish: reveal animations (guess vs. actual pin +
  connecting line, score-count-up), competitive leaderboards/streaks, non-competitive
  "just play" mode framing, and any other GeoGuessr-parity features not covered above.
