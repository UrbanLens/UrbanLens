# SpotGuessr (UL-391..UL-396)

Status: DRAFT — rules normalized from product notes 2026-07-24. Phase 1 (UL-391) shipped
2026-07-24. Phase 2 (UL-392, multiplayer) and Phase 3 (UL-393, Named Place + Street View
modes) are being built now; UL-394..UL-396 remain follow-up tickets.

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
5. **Mode-specific data exists** — Photos mode requires at least one eligible `Image` (see
   Photo selection below); Named Place mode requires a *meaningful* wiki name or alias (see
   Named Place selection); Street View mode requires the Street View Static API to report
   imagery coverage near the location (see Street View selection).
6. **Joined, not just invited** (multiplayer only) — "every participant" means every profile
   with `GameSessionParticipant.status = JOINED`. An invited-but-not-yet-accepted profile is
   not yet a player and does not gate eligibility on their pins — see "Multiplayer sessions."

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
near            = exp(-distance_km / NEAR_DECAY_KM)
city            = exp(-distance_km / CITY_DECAY_KM)
location_points = round(MAX_ROUND_POINTS * (NEAR_WEIGHT * near + (1 - NEAR_WEIGHT) * city))   # floor 0
date_points     = round(MAX_DATE_POINTS  * exp(-abs(days_off) / DATE_DECAY_DAYS))              # floor 0, only when date guessing is on
```

A single exponential decay makes anything past ~10-15km read as an undifferentiated zero,
which feels unfairly harsh — a guess in the right city, or even just a few blocks off, should
still feel like progress, while full points remain reserved for landing at (or inside) the
target boundary. Blending a fast near-field decay (`NEAR_DECAY_KM`, rewards "a few blocks"
precision) with a slow city-scale decay (`CITY_DECAY_KM`, keeps "same city" meaningfully
non-zero) achieves that without flattening the curve near the target. `location_points` is
what feeds the Glicko-2 rating update (see below); `date_points` is purely a side-score, never
mixed into skill rating — guessing well from a photo and guessing well from EXIF-adjacent
reasoning ("this car model dates the photo") are different skills, and conflating them would
make the core rating noisier for players who never enable date guessing.

### Country/state/city bonus points

`services.spotguessr.geo_bonus` adds a small, independent bonus on top of `location_points` for
guessing the right general area, even when the exact guess is off — reducing how often a round
reads as "basically zero." A guess's reverse-geocoded country/state/city (via
`NominatimGateway.reverse_geocode_admin`) is compared against the answer location's own stored
`country`/`state`/`city`; each matching tier stacks (`COUNTRY_BONUS` + `STATE_BONUS` +
`CITY_BONUS` all apply for a spot-on city guess). A tier is only offered when the session's
eligible-location pool actually varies on it (`bonus_scope_for()`, computed once at round 1 and
frozen on `GameSession.config["bonus_scope"]`) — a session already constrained to one state (by
`geo_bounds`, `require_visited_all`, or simply a small pin pool) doesn't hand out a "right
state" bonus for free. Stored on `Guess.bonus_points` and, unlike `date_points`, folded into
the Glicko-2 outcome fraction — admin-area correctness is the same "know where this is" skill
the rating measures.

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

`services.spotguessr.photos.candidate_image_for_location()` pulls from `Image` rows on the
location (`Image.location_id = location.id`, `media_type=photo`).

**Privacy invariant (non-negotiable): `Image.wiki_id` must be set.** An earlier version of
this gate reasoned "no separate opted-into-the-game gate is needed in Phase 1 - it's the
player's own pins/photos, or public wiki photos" - that reasoning was wrong even for solo
play: eligibility only requires the *location* be pinned by every participant, not that a
given *photo* belongs to them, so any other profile's private pin photo at that same location
was equally eligible, with no sharing action behind it at all. `Image.wiki` being null must
always be read as "not eligible," never as "eligible with a caveat" - a photo only reaches a
wiki through an explicit share (or, once the game gets its own upload flow, by attaching to
the wiki at upload time), which is the one signal that reliably means a profile chose to make
it public. See `services.spotguessr.photos`'s module docstring for the same invariant stated
at the code level.

### Photo relevance feedback

The game's secondary goal (besides being fun) is generating data that improves the site's own
notion of which wiki photos are actually good - see "External media caching + relevance"
below for the full design. Briefly: `GamePhotoFeedback` records one event per (round, guessing
profile) - `thumbs_up`, `thumbs_down`, `reported`, or a server-backfilled `no_reaction` for
anyone who never explicitly reacted (`services.spotguessr.relevance`). `services.media_relevance
.effective_relevance()` blends this with the wiki's own organic `MediaRelevance` votes:

| Signal | Weight | Why |
|---|---|---|
| Wiki thumbs up/down (existing) | ±1.0 | the wiki's own organic signal, unchanged |
| In-game thumbs up | +0.5 | deliberate positive reaction, but weaker than a wiki visitor voting on this exact photo |
| In-game report | -1.0 | a direct "this doesn't belong here" claim - same weight as a wiki downvote |
| In-game thumbs down | -0.001 (token weight) | means "wrong photo for this game" (blurry, gives away the answer, ambiguous), not "not relevant to this wiki" - a real but deliberately tiny nudge, never enough by itself to drag a genuinely relevant photo's score to/below zero and knock it out of eligibility; intended to matter only for a future "order relevant photos by how relevant" ranking, not for the eligibility filter below |
| Shown, no reaction | +0.01 | most impressions are silent; only meaningful in aggregate over many plays, and is exactly what lets a never-voted photo bootstrap a score |

`config.allow_arbitrary_external_photos` (default **off**) controls whether an externally-
sourced candidate must have a non-negative `effective_relevance` score to be shown. Off is
non-negative rather than requiring strictly positive, since almost every wiki photo starts at
exactly 0 (no votes yet) and it's this very mode that's expected to grow that score. On lifts
the filter entirely, including for photos already known to be bad - trading photo quality for
pool size, and deliberately generating gameplay signal on exactly the photos that most need
it. Personal uploads shared to the wiki are always eligible either way - they were never
rendered through the Media gallery, so they have no relevance identity to score.

## Crowd-sourced photo coordinates

A photo shown in a Photos-mode round with no coordinates of its own scores
against the location's boundary (see "Scoring" above) - which means every
such guess is, incidentally, also a guess at where the photo itself actually
is. `services.spotguessr.photo_coordinates.record_guess()` captures that
signal anonymously: **every** Photos-mode guess becomes a
`PhotoCoordinateGuess` row - the guessed point, a correct/incorrect flag,
and a timestamp - regardless of whether the photo already has its own
coordinates. Currently-placed photos' guesses aren't used for anything
(the estimate below would be moot for them, since a real coordinate already
wins), but they're saved anyway on the theory they may be useful later -
e.g. flagging or correcting a coordinate that turns out to be wrong. "Correct"
reuses the guess's already-computed distance rather than a second lookup
(0 or less): for a boundary-target round that's "inside the location's
boundary", for a point-target round that's "guessed the exact point" (a much
rarer bar, but the same underlying value) - deliberately independent of the
round's own scoring either way; it only decides whether a guess counts
toward the average below. **No profile, round, or session FK at all** -
structurally, not just by convention, there is no way to trace a row back
to who made it.

Once a photo *without its own coordinates* has 5+ correct guesses,
`services.photo_coordinates.recompute_estimated_coordinates()` averages
them into `Image.estimated_latitude`/`estimated_longitude`, recomputed
fresh from the photo's full correct-guess set after every new correct guess
(not incrementally) - per-photo guess volume is small enough that this
stays cheap, and it's simpler to keep correct as the outlier trim's
boundary shifts with more data than to maintain running-average
bookkeeping. A point-target round's guesses are recorded but never trigger
this recompute, since there's nothing for the estimate to do once a real
coordinate already exists. Once there are 10+ correct guesses (enough for
"outlier" to mean anything), before averaging it drops the ~15% of points
farthest from the overall centroid - a loose, cheap trim (plain lat/lng
degree-distance, not a real geodesic one), not a rigorous statistical test,
matched to "our best effort to approximate a position" rather than precision.

`Image.effective_latitude`/`effective_longitude` read this as a middle tier:
a real (manual or EXIF) coordinate always wins outright, no matter how many
guesses back the estimate; then the estimate, if one exists; then the
shared Location's own coordinates as a last resort. This is what lets a
still-unplaced photo show up (approximately) on the site's maps at all -
which is the point: surfacing it is what gets a wiki user to notice and
correct the exact placement, rather than the photo staying invisible until
someone manually places it first.

## External media caching + relevance

Not SpotGuessr-specific, but implemented alongside it since the game's relevance feedback
needed a reliable way to join a materialized photo back to its wiki votes. Two independent
fixes/additions to the existing pin-detail/wiki Media gallery (`services.media_materialize`,
`services.media_relevance`):

- **Identity fix.** `Image.media_source_key`/`Image.media_item_key` now store the *raw*
  provider panel key and the sha1 hash of the item's raw full-resolution url - exactly the
  `(source, item_key)` identity `MediaRelevance` marks are keyed by. `Image.source_url` can't
  serve this purpose: it's set to `page_url or url`, which diverges from the raw `url` that
  `media_item_key()` is always hashed from whenever a provider supplies a `page_url`. Without
  this, a materialized row had no reliable way to be joined back to its own votes at all.
  Fixed alongside it: `materialize_media_item`'s dedupe filter compared the *raw* panel key
  against `Image.source`, which stores the *translated* `ImageSource` value - for any source
  with a translation (`"loc"` -> `library_of_congress`, `"cris_building"` -> `cris`), that
  mismatch meant the filter could never match a previously materialized row, so every repeat
  vote re-downloaded and duplicated it.
- **Local-copy preference.** Marking an external Media gallery item "relevant" already
  downloaded and materialized it into a durable `Image` row (existed before this pass, on the
  pin-detail page only). What was missing: the gallery never *served* that local copy back -
  every subsequent page load, by anyone, still hot-linked the live provider url straight from
  `LocationCache`. `services.media_relevance.local_images_for_gallery_items()` bulk-looks-up
  already-materialized rows for a panel's live results; both the pin-detail and wiki Media
  views now prefer that local url for the displayed thumbnail/image, falling back to the
  remote url when no local copy exists yet. The remote page (`item.page_url|default:item.url`)
  stays the "Open source" link regardless, so the original is never dropped - only the default
  display source changes once a cached copy exists.

## Solo vs. multiplayer

`GameSession` and `GameSessionParticipant` were modeled as a proper many-participant session
from Phase 1 (not a "solo-only" shape retrofitted later) because every eligibility/scoring
rule already reads "all participants," not "the player." Phase 1 only ever created
single-participant sessions; UL-392 (below) adds the invite/join/real-time-sync flow on top
of the same tables — no schema changes to the round/guess/rating path were needed, only a
`status` field on the participant row and two new models (chat, and nothing else).

## Multiplayer sessions (UL-392)

### Lobby lifecycle

A new `GameSessionStatus.LOBBY` precedes `ACTIVE`: a multiplayer session is created in
`LOBBY`, sits there while people join, and only transitions to `ACTIVE` (creating round 1)
when the host explicitly starts it. Solo sessions skip the lobby entirely — `start_solo_session`
still creates an `ACTIVE` session with one `JOINED` participant, unchanged from Phase 1.

```
(created) → LOBBY ──host starts──▶ ACTIVE ──all rounds played──▶ COMPLETED
                                       │
                                       └──no eligible locations remain──▶ COMPLETED (early)
```

No `ABANDONED` transition is added by this phase — an inactive lobby just sits in `LOBBY`
forever, harmlessly (see "not built" below).

### Participants: invited vs. joined

`GameSessionParticipant.status` (new field, `GameSessionParticipantStatus`: `INVITED` |
`JOINED`) mirrors `TripMembership.status` exactly (`models/trips/model.py`) — the same
model row that will later hold the accepted membership *is* the invite record, there is no
separate `GameSessionInvite` model. The host's own row is created as `JOINED` immediately
(`start_multiplayer_session`); every invitee's row is created as `INVITED`.

- **Inviting**: friends-only, matching the trip-invite precedent (`controllers/trip.py`) —
  `services.connections.get_connections(host)` gates who can be picked. Inviting a non-friend
  is rejected server-side, not just hidden in the picker UI. One `GameSessionParticipant`
  per invitee, plus one `NotificationLog` (`NotificationType.SPOTGUESSR_INVITE`) that reaches
  the invitee live via the existing `notification_new` Channels push — no new notification
  infra needed, just a new `NotificationType` value and a creation call, same shape as
  `_notify_added_to_trip`.
- **Joining**: `POST` flips `INVITED → JOINED` and broadcasts `participant.joined` to the
  session's WebSocket group. A profile can only join a session they were invited to (404
  otherwise, same not-found-not-forbidden convention as everywhere else in this feature).
- **Starting**: host-only. Locks the roster — **no one can join after round 1 exists**. This
  is a deliberate simplification (see "Explicitly not built" below): GeoGuessr-style private
  lobbies don't support mid-game joins either, and supporting it here would mean a late
  joiner either replays already-finished rounds (confusing) or skips them (breaks the
  "N rounds, everyone plays the same N" scoreboard invariant).
- **Eligibility uses only `JOINED` participants** — an invitee who never accepts must not
  gate what locations are playable (rule 6 in "Eligibility" above). Similarly, "has everyone
  guessed this round" and the final scoreboard both read `JOINED` participants only.

### Real-time sync: `GameSessionConsumer`

One `AsyncWebsocketConsumer`, one channel-layer group per session
(`f"spotguessr_session_{session_id}"`) — modeled directly on `SafetyCheckinChatConsumer`
(`dashboard/consumers.py`), **not** `DirectMessageConsumer`'s per-profile-group shape, since
every participant genuinely needs the same broadcast (unlike a DM/group-chat's per-viewer
identity-masked payloads, which don't apply here — session participants already see each
other by name on the scoreboard). Route: `ws/spotguessr/session/<int:session_id>/`.

`connect()` requires the connecting profile to already be a `GameSessionParticipant` (any
status — an invitee should see the live lobby fill up before accepting) — 404-equivalent
close code `4404` otherwise, `4500` for unexpected errors, matching every other consumer's
convention.

Broadcast event types (channel-layer `type`, dot-notation dispatched to `snake_case` handler
methods per Channels convention):

| Event | Sent when | Payload |
|---|---|---|
| `participant.joined` | An invitee accepts | profile id/username |
| `session.started` | Host begins the game | round 1 (safe-serialized, no answer) |
| `guess.submitted` | Any participant guesses | which profile, so others see "waiting on 2 more" — **not** their guess coordinates or score, which stay hidden until reveal |
| `round.revealed` | The **last** joined participant guesses that round | the answer, every participant's distance/points, updated scoreboard totals |
| `round.started` | The next round is generated (server-driven, right after a reveal broadcast) | round N (safe-serialized) |
| `session.completed` | Final round revealed | full summary (same shape as `session_summary()`) |
| `chat.message` | Any participant sends chat | sender, body, timestamp |

The HTTP endpoints from Phase 1 (`start`/`round`/`guess`/`summary`) are unchanged and still
work standalone (a solo session never opens a WebSocket at all) — multiplayer sessions use
both: HTTP for the action that changes state (submitting a guess is still a POST, since it
must be durably recorded even if the WebSocket briefly drops), and the WebSocket purely to
*fan out* what happened to everyone else. `submit_guess()` in `services.spotguessr.session`
is the single choke point that decides when a round is fully guessed and triggers both the
rating update (unchanged from Phase 1) and the `round.revealed`/`round.started` broadcast.

### Session chat

`GameSessionChatMessage` (new model: `session` FK, `profile` FK, `body`, `created`) — plain
text, no E2EE. Unlike `DirectMessage`/`GroupMessage`, session chat is inherently ephemeral
match banter between people already visible to each other on the scoreboard, not a private
conversation — the encryption machinery those models carry doesn't buy anything here and
would add real complexity (key exchange, ciphertext fallback UI) for no privacy benefit.
Sent and broadcast over the WebSocket only (`chat.message` incoming → save → broadcast) — no
HTTP fallback send path in this phase (see "not built" below); read history is served over
HTTP (`GET` the last N messages) so reconnecting/late-opening the page shows recent context.

### Explicitly not built in UL-392 (tracked as follow-up, not silently dropped)

- **Join-by-link** (inviting someone who isn't yet a friend). Friends-only matches this
  app's existing invite model everywhere else and avoids a new "who can see this session
  exists" exposure surface; revisit only if user feedback specifically asks for it.
- **Mid-game joining / reconnecting after a dropped roster.** A session's roster is fixed at
  `session.started`.
- **Session abandonment/cleanup** for lobbies nobody ever starts, or active sessions where a
  participant goes AFK forever (no timeout, no host-skip-round control). A stuck round simply
  waits; the host can't currently force a reveal. Worth a follow-up if this proves annoying
  in practice.
- **HTTP fallback for sending chat** (mirroring `ws/messages/`'s pattern) — WebSocket-only
  for now, consistent with keeping this phase's scope to what multiplayer actually needs.
- **Voice chat** — still UL-395, unchanged.

## Named Place and Street View modes (UL-393)

### Named Place mode

**Selection** (`services.spotguessr.named_place.candidate_name_for_location`): the location
needs a *meaningful* name to show. Reuses `services.public_pins.is_meaningful_name()`
verbatim (already filters blank/placeholder/coordinate-shaped strings — see
`docs/designs/public-pins-by-vote.md`) rather than inventing a second heuristic:

1. If `config.use_aliases` (default **True** — per spec, aliases are on by default with a
   setting to turn them off) and the wiki has at least one meaningful `WikiAlias`, pick one
   at random.
2. Otherwise, fall back to `wiki.name` if meaningful.
3. If neither is meaningful, the location is **not eligible** for a Named Place round this
   session (excluded the same way Photos mode excludes a location with no usable photo —
   `get_or_create_round` tries the next candidate rather than surfacing an unplayable round).

No photo, no `Image` row involved at all — `GameRound.image` stays null for this mode.

**Scoring**: always boundary-distance (`target_is_point = False`), never a point — this is
the mode's whole reason to exist, restated from the "Scoring" section above: a name names a
*place*, not a coordinate.

**No search** — per spec, Named Place mode's guess UI is map-click only, with no "search my
pins" affordance (the point of the mode is testing whether the player recognizes the name
*without* being able to just look it up in their own pin list). The client omits the pin
search box entirely when `round.mode == "named_place"`.

### Street View mode

**Selection** (`services.spotguessr.street_view.candidate_street_view_for_location`): calls
the existing `GoogleMapsGateway.get_street_view_single()` (`services/apis/locations/google/
maps.py`) — the same server-side fetch already used for the pin-detail Street View carousel,
including its coverage-metadata check and radius-expansion search. Returns a base64
`data:image/jpeg;base64,...` URI (never a client-exposed API key, never a raw Google Maps
embed) or `None` if there's no coverage nearby, in which case the location is excluded from
this session's Street View rounds the same way an image-less location is excluded from
Photos mode. Wrapped in a broad `except Exception` at the call site — this is a paid,
rate-limited external API on the critical path of picking a round, and a transient failure
must degrade to "try another location," never crash round generation.

**Scoring**: point-based (`target_is_point = True`), using the *location's own* `point` as
the target coordinate (there is no `Image` row to carry a more specific point — Street View
imagery is definitionally centered on the location's coordinate). Distance therefore behaves
exactly like a coordinate-bearing photo, per the "Scoring" section above.

**Search allowed** — like Photos mode (and unlike Named Place), the player may click the map
or search their own pins.

**Cost note**: Street View Static API calls are billed per request. `get_street_view_single()`
already caches (`StreetViewProvider.get_street_view_slides()`'s
`make_cache_key`/`SiteSettings.external_data_cache_days` wrapper), so repeat rounds at the
same location within the cache window don't re-bill — no additional caching added here.

### Shared plumbing change

`get_or_create_round` (`services.spotguessr.session`) gained a per-mode branch: Photos calls
`photos.candidate_image_for_location`, Named Place calls
`named_place.candidate_name_for_location`, Street View calls
`street_view.candidate_street_view_for_location`. All three return "nothing usable" as
`None`/falsy and are handled identically — try the next candidate location, give up after
`_MAX_LOCATION_ATTEMPTS`. `start_solo_session`'s Phase 1 restriction to `SpotGuessrMode.PHOTOS`
is lifted; all three modes are now startable, solo or multiplayer.

## Config defaults (tunable — one dataclass, `SpotGuessrConfig`)

| Constant | Default | Note |
|---|---|---|
| `MAX_ROUND_POINTS` | 5000 | GeoGuessr-familiar scale |
| `NEAR_DECAY_KM` | 1.5 | "a few blocks" reads as excellent |
| `CITY_DECAY_KM` | 40.0 | "same city" stays meaningfully non-zero |
| `NEAR_WEIGHT` | 0.65 | blend toward near-field precision |
| `MAX_DATE_POINTS` | 1000 | secondary to location score |
| `DATE_DECAY_DAYS` | 180 | half a year |
| `COUNTRY_BONUS` | 100 | `services.spotguessr.geo_bonus` |
| `STATE_BONUS` | 250 | `services.spotguessr.geo_bonus` |
| `CITY_BONUS` | 400 | `services.spotguessr.geo_bonus` |
| `DEFAULT_ROUNDS_PER_SESSION` | 5 | |
| `MIN_ROUNDS_PER_SESSION` / `MAX_ROUNDS_PER_SESSION` | 3 / 20 | |
| `MIN_LOCATION_RATING` / `MAX_LOCATION_RATING` | 1000 / 2000 | difficulty-slider target band |
| `DIFFICULTY_BANDWIDTH` | 200 | Gaussian kernel width, in rating points |
| `MIN_GAMES_FOR_DIFFICULTY_WEIGHTING` | 5 | below this, treat as neutral (1500) |
| `MIN_SEPARATION_KM` | 0.5 | anti-clustering exclusion radius from the previous round |
| Glicko-2: rating / RD / volatility / scale / τ | 1500 / 350 / 0.06 / 173.7178 / 0.5 | Glickman's published defaults |
| `use_aliases` | True | Named Place mode; per-session config, not a site-wide constant |
| `allow_arbitrary_external_photos` | False | Photos mode; skips the relevance filter entirely when true - see "Photo relevance feedback" |
| `GAME_THUMBS_UP_WEIGHT` / `GAME_REPORT_WEIGHT` / `GAME_NO_REACTION_WEIGHT` / `GAME_THUMBS_DOWN_WEIGHT` | 0.5 / 1.0 / 0.01 / 0.001 | `services.media_relevance` - thumbs down is a token weight, not a real "not relevant" vote |
| `MIN_GUESSES_FOR_ESTIMATE` / `MIN_GUESSES_FOR_OUTLIER_TRIM` / `OUTLIER_TRIM_FRACTION` | 5 / 10 / 0.15 | `services.photo_coordinates` - crowd-sourced unplaced-photo coordinates |
| `CHAT_HISTORY_LIMIT` | 50 | messages returned by the chat-history GET on reconnect |

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

- **UL-391 (shipped 2026-07-24)** — data model (ratings, session, round, guess, preference);
  Glicko-2 engine; eligibility engine; point/boundary distance scoring; date-guessing bonus;
  difficulty slider; geographic boundary filter; anti-clustering selection; solo-only Photos
  mode end to end (start session → round → guess → reveal → summary); own-rating + friends'
  -ratings-with-opt-out display. Models: `models/spotguessr/`. Services:
  `services/spotguessr/`. Controller: `controllers/spotguessr.py`. UI: Leaflet click-to-guess
  map + pin-search, `frontend/ts/entries/spotguessr.ts`.
- **UL-392 (this pass)** — multiplayer sessions: friends-only invite/join lobby
  (`GameSessionParticipant.status`), a `GameSessionConsumer` (Channels, one group per
  session) for round sync/live scoreboard, and WebSocket-only live text chat
  (`GameSessionChatMessage`). See "Multiplayer sessions" above for the full design and its
  explicit non-goals (join-by-link, mid-game joining, session cleanup/timeouts, chat's HTTP
  fallback — all deferred, not silently dropped).
- **UL-393 (this pass)** — Named Place mode (boundary-distance guessing from a name/alias,
  no search, aliases on by default with a setting to disable them) and Street View mode
  (point-distance, reusing the existing `GoogleMapsGateway` Street View integration). See
  "Named Place and Street View modes" above.
- **Photo privacy fix + relevance feedback (this pass)** — found and fixed a real privacy gap:
  Photos-mode round generation had no gate at all on which `Image` rows it could show, so a
  private pin's photo (or, in principle, a safety check-in photo) could reach another
  profile's game session with no sharing action behind it. `candidate_image_for_location` now
  unconditionally requires `wiki__isnull=False` — see "Photo selection" above. Also built the
  in-game thumbs-up/thumbs-down/report feedback buttons and their weighted contribution to
  `services.media_relevance.effective_relevance` (`GamePhotoFeedback`,
  `services.spotguessr.relevance`), the `allow_arbitrary_external_photos` setting, and — not
  SpotGuessr-specific, but needed to make relevance joinable at all — the `Image.media_source_key`/
  `media_item_key` identity fields and local-copy-preferred serving in the pin-detail/wiki
  Media gallery. See "Photo relevance feedback" and "External media caching + relevance" above.
- **UL-394 (remaining follow-up)** — community photo submission pipeline: upload-to-wiki with a
  submit-to-game checkbox (and an upload notice that it was added to the location's wiki,
  which — per the new privacy invariant above — is also what makes it eligible for the game),
  a "submit this wiki photo to the game" button in the lightbox, a post-reveal "wrong location
  for this photo" flag distinct from the in-game report already built (that one means "doesn't
  belong here at all"; this one means "right place, but not this exact spot"), and the
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
