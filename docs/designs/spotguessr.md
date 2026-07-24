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
| `use_aliases` | True | Named Place mode; per-session config, not a site-wide constant |
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
