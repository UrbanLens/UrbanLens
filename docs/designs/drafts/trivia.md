# Trivia

Status: DRAFT — written 2026-07-25 to document an already-shipped feature (built across four
commits on 2026-07-24/25: schema + solo play, multiplayer + chat, user submission + AI
moderation + AI generation, AI wiki incorporation). No prior design doc existed for this
feature despite it having reached feature-complete status — see `docs/FEATURES.md`'s "Games:
Trivia" section for the current build/not-built line. Gated behind `SiteFeature.ALPHA_FEATURES`
on the games hub (`controllers/games.py`), same as SpotGuessr.

## Goal

A quiz game built on UrbanLens's own pin/wiki/location data: players answer questions about
places they've pinned, using only locations every participant already knows (has pinned) —
the same core eligibility rule SpotGuessr uses. Questions come from three sources (user
submissions, AI mined from wiki articles, deterministic templates from structured property
data), all funneled through a single content classifier before ever reaching a player. Player
skill and question difficulty are tracked with Glicko-2, mirroring SpotGuessr's player/location
pairing exactly, so a difficulty slider has a real target to weight against.

## Vocabulary

- **Location** / **Pin** — same shared models as SpotGuessr; "pinned by everyone in the
  session" is reused outright (`services.spotguessr.eligibility.eligible_locations`), not
  reimplemented.
- **TriviaQuestion** — one question about a location: a prompt, a canonical answer, a source
  (`user_submitted` / `ai_generated` / `deterministic`), and a moderation status (`pending_review`
  / `approved` / `rejected`).
- **TriviaSession** / **TriviaRound** / **TriviaAnswer** — one playthrough, one question asked
  within it, one participant's answer to it — the direct analogs of `GameSession` / `GameRound`
  / `Guess`.
- **Player rating** — a profile's overall Glicko-2 skill rating (`PlayerTriviaRating`). Unlike
  SpotGuessr, there is no per-mode split — Trivia has no notion of separate modes.
- **Question rating** — a question's Glicko-2 *difficulty* rating (`TriviaQuestionRating`), the
  direct analog of `LocationModeRating`.

## Eligibility

A question is eligible for a round in session S iff:

1. **Its location is pinned by every (joined) participant** — delegates outright to
   `services.spotguessr.eligibility.eligible_locations`, the exact same rule SpotGuessr enforces,
   since this is a Location/Pin concept unrelated to which game is played on top of it.
2. **`status == APPROVED`** — a `pending_review` or `rejected` question is never shown to anyone
   except, very rarely, its own submitter in solo play (see "No feedback loop for submitters"
   below) — a deliberate, narrow exception, not a general rule.
3. **In rotation** — `services.trivia.voting.effective_score(question) >= 0`. A question whose
   score climbs back to non-negative later naturally re-enters rotation; there is no separate
   permanent-retirement state.
4. **Not already asked earlier in this session** — no repeats within one playthrough, mirroring
   SpotGuessr's "not already used" rule. Unlike SpotGuessr, there is no spatial anti-clustering
   rule on top of this — a text question doesn't have the same "same block twice" problem a map
   guess does.
5. **Inside the configured geographic boundary** (optional, `config.geo_bounds_geojson`).
6. **Joined, not just invited** (multiplayer only) — mirrors SpotGuessr's rule 6 exactly.

`services.trivia.eligibility.eligible_questions()` also materializes deterministic questions
on demand for each candidate location as it's considered, so a freshly-pinned location with no
question rows yet becomes eligible the first time it's checked, with no separate backfill job
needed.

## Question sources

### 1. Deterministic (`services.trivia.deterministic`)

Template questions generated from `LocationCache` property-records data
(`services.locations.site_scope.parcel_buildings()`), read-only against whatever's already
cached — never triggers a live REData fetch. A location with no cached data, or data missing
the fields a generator needs, simply yields no question from that generator; this is expected
(year-built/building-number data is only reliably populated for CRIS-sourced NY buildings
today), not an error. Idempotent per `(location, dedupe_key)`, so calling it repeatedly (e.g.
once per round-candidate evaluation) never duplicates rows. Created `APPROVED` directly — a
template built from structured data carries none of the person/bullying/in-group risk the
classifier exists to catch, so it skips classification entirely.

Generators built:

- **Year built** — one question per named building with a known `year_built`.
- **Building number** — one question per named building with a known `building_number`.
- **Building count** — "how many buildings are on this parcel," only once the count itself is
  a genuinely interesting fact (`BUILDING_COUNT_QUESTION_THRESHOLD = 4` — deliberately stricter
  than `site_scope.MULTI_BUILDING_THRESHOLD`'s bar of "is this multi-building at all").

Both name-keyed generators skip any building without a *meaningful* name
(`services.locations.naming.is_meaningful_name`), the same heuristic SpotGuessr's Named Place
mode uses — a question about "Building #3" (an auto-generated placeholder label) isn't a fact
worth asking about.

### 2. AI-generated from wiki articles (`services.trivia.generation`)

Mines a wiki's `description` (must be at least `MIN_DESCRIPTION_LENGTH = 400` characters —
"substantial content" worth mining) via a dedicated AI feature (`trivia_generation`), asking
for up to `MAX_QUESTIONS_PER_WIKI = 3` question/answer pairs based only on facts stated in the
article. Idempotent per location: a location that already has at least one `AI_GENERATED`
question is skipped entirely on future sweeps. Every candidate is classified through the exact
same shared classifier as a user submission (see below) before ever being persisted — a
rejected candidate was never shown to anyone, so unlike a user's own rejected submission there
is no "show it back to the author very rarely" leniency to apply; it's simply discarded.
Driven by a scheduled Celery task (`tasks.run_scheduled_trivia_generation`, hourly, single-flight
cache-locked), not a wiki-save hook — there is no existing "wiki saved" action hook in the
plugin system to react to instead (see `docs/designs/plugins.md`'s hook bus), and a batched hourly sweep
naturally caps AI spend.

### 3. User-submitted (`services.trivia.submission`)

A profile that has pinned a location may submit a question about it
(`TriviaQuestionSubmitView`, `POST /games/trivia/questions/submit/`). Created `PENDING_REVIEW`
immediately, then classified asynchronously on commit (`tasks.classify_trivia_submission`) so a
rolled-back transaction never schedules a task against a row that was never actually saved.

## Content classifier (`services.trivia.classifier`)

The single highest-harm piece of the feature — a false negative lets a bullying/in-group
question reach real users with no report path tracing back to "the filter missed this"; a false
positive silently kills a legitimate question with, by design, no feedback loop for the author
to notice or correct. **Used identically for both a user-submitted question and an
AI-generated one** — the same rules, the same code path, per spec.

Every failure mode defaults to reject (fail closed): AI globally/per-profile disabled, a
transport-level failure, an empty response, or an unparseable response are all treated as a
rejection, never an approval. Follows `services.labels.auto_tag`'s allowlisted-`<ANSWER>` pattern — the
model must answer with exactly one of a fixed set of tokens.

Reject categories:

- **`REJECT_PERSON`** — the question is about, or centers on, a specific individual, **even if
  only referenced indirectly and never named** (e.g. "What was X in the year *somebody* did Y?")
  — members of a small community will very likely know exactly who is meant even without a name.
- **`REJECT_BULLYING`** — an insulting, mocking, or bullying adjective or description, even with
  no specific target named.
- **`REJECT_INGROUP`** — references a specific exploring group, crew, team, or "party" rather
  than the location itself (e.g. "When was the first party at X?") — knowledge only accessible
  to insiders, not the general trivia audience.
- **`REJECT_OFF_TOPIC`** — not actually about a location, its history, or its architecture.

An approved question must be about the location itself — history, architecture, construction
dates, ownership, structural facts, geography, or similar. "When in doubt, reject" is stated
explicitly in the classifier's own instructions: the cost of a missed good question is much
lower than the cost of one that embarrasses or excludes people.

### No feedback loop for submitters

A user is never told whether their submitted question was approved or rejected — the response
to a submission is always just `{"submitted": True}`, and `rejection_reason` is internal-only
(never serialized to the submitter). This is deliberate: without a feedback signal, a submitter
can't iteratively tweak a rejected question until it slips past the filter. The one narrow
exception: **a solo player's own not-yet-approved question (`PENDING_REVIEW` or `REJECTED`) may
still appear to them, very rarely, in solo play only** — weighted at `OWN_UNAPPROVED_WEIGHT =
0.03` relative to normal selection weight (`services.trivia.eligibility.solo_own_pending_questions`,
wired into `services.trivia.selection.pick_next_question` via a per-question weight override).
It is never shown to any other player, and never included at all in a multiplayer session.

## Answer checking

`TriviaQuestion.answer_normalized` (computed in `save()`) is the canonical answer, casefolded
and stripped to only letters/digits (`TriviaQuestion.normalize_answer`). An incoming answer is
normalized the same way and exact-compared first (`TriviaAnswerMatchKind.EXACT`).

Only on a mismatch does `services.trivia.answer_check.is_answer_equivalent` consult AI: gated on
`SiteFeature.AI` (a profile without that subscription feature always falls back to
exact-match-only, never blocked from playing), asks whether the two answers mean the same thing
just phrased differently (capitalization, abbreviation, a nickname, extra/missing words) — not
whether they're merely related or partially correct. Fails closed to "not equivalent" on any AI
unavailability or unparseable response; an AI-judged match is recorded as
`TriviaAnswerMatchKind.AI`. Answering doesn't award partial credit either way — a flat
`POINTS_FOR_CORRECT_ANSWER = 1000` for correct, 0 for incorrect, whether matched exactly or via
AI.

## Voting and rotation (`services.trivia.voting`)

One vote table (`TriviaQuestionVote`), an event log keyed `(question, profile)` — a fresh
`NO_REACTION` backfill each time a profile is asked the same question again in a later session,
since the signal is only meaningful in aggregate over many plays.

| Kind | Weight | Note |
|---|---|---|
| Upvote | +1.0 | |
| Downvote | -1.0 | Deliberately real weight, unlike SpotGuessr's near-zero photo-thumbs-down — per spec, downvotes alone can retire a question from rotation. |
| Report | -3.0 | Outweighs a plain downvote — "this is a problem," not just "I didn't enjoy this one." |
| No reaction | +0.05 | Exact value from spec — a question shown with no explicit reaction earns a small passive-positive signal, letting a never-voted question bootstrap a non-negative score. |

`effective_score()` sums these; `eligible_questions()` gates rotation at `>= 0` (see
"Eligibility" above). Recording a vote always overwrites whatever was previously recorded for
that `(question, profile)` pair (`update_or_create`), including a prior `NO_REACTION` backfill
or an earlier change of mind; the backfill itself uses `get_or_create` so it can never clobber
an explicit reaction a fast player already submitted before the round finished for everyone.
Voting is restricted to a question the profile has actually been asked at least once
(`TriviaQuestionVoteView`), mirroring `SpotGuessrPhotoFeedbackView`'s "you can only react to a
round you've guessed on" rule.

## Glicko-2 ratings: player skill vs. question difficulty

Mirrors `services.spotguessr.ratings.apply_round_ratings` exactly, reusing
`services.spotguessr.glicko2`'s pure math directly rather than reimplementing it — same
defaults (rating 1500, RD 350, volatility 0.06, scale 173.7178, τ=0.5). The one difference: a
round's outcome is binary (1.0 for a correct answer, 0.0 for incorrect) rather than SpotGuessr's
continuous distance-based fraction — an AI-judged "close enough" match still counts as a full
1.0, it doesn't introduce partial credit into the rating either.

A round is one rating period for both sides: each player's `PlayerTriviaRating` updates once
with the question as sole opponent; the question's `TriviaQuestionRating` updates once with
every participant as opponents, each with outcome `1 - (whether they answered correctly)` — a
question nobody can answer is "winning" against the field, exactly the high-difficulty signal a
hard question should earn. Both sides read the pre-round rating of the other, captured once up
front, per Glicko-2's requirement that both sides of a period use each other's start-of-period
ratings.

## Difficulty slider

Same shape as SpotGuessr's, applied per-*question* rather than per-location: the same location
can host a trivial building-count question and a hard year-built question, so difficulty is a
question-level knob here, not a location-level one.
`target_rating_for_difficulty(difficulty)` linearly maps a 0.0–1.0 slider to
`MIN_QUESTION_RATING`–`MAX_QUESTION_RATING`; candidates are then weighted by a Gaussian kernel on
`|question_rating - target_rating|` (`DIFFICULTY_BANDWIDTH`). A question with fewer than
`MIN_GAMES_FOR_DIFFICULTY_WEIGHTING = 5` plays stays at the neutral default rating (1500) rather
than being excluded for lack of data — same "never penalized for lack of history" contract as
SpotGuessr's location-rating kernel, though Trivia has no proxy-seeding equivalent (SpotGuessr
seeds from pin/photo counts; a question has no comparable proxy signal before it's ever played).

## AI wiki incorporation (`services.trivia.wiki_incorporation`)

Once a `USER_SUBMITTED`, `APPROVED` question's `effective_score()` crosses
`WIKI_INCORPORATION_SCORE_THRESHOLD = 5.0` — set well above the `>= 0` rotation gate, so only
genuinely well-received questions, not merely non-negative ones, ever reach the wiki — an AI
writing agent drafts one short plain-text paragraph folding the fact into the location's wiki
article. Reuses the exact same draft → sanitize → safety-classify → append pipeline as
`services.ai.article_expansion` (same `sanitize_article_plain_text`,
`article_safety.classify_article_text`, `article_expansion.append_to_article`) — the safety bar
for what belongs in a wiki article doesn't depend on where the source material came from, so
that classifier is shared outright, not duplicated. Only the writing step itself carries a
dedicated AI feature (`trivia_wiki_incorporation`) for its own SiteSettings toggle and cost
tracking.

`TriviaQuestion.wiki_incorporated_at` is set once a question is fully processed — appended,
safety-rejected, or "nothing new to say" — so it's never proposed to the writing model a second
time. An AI-unavailable outcome deliberately leaves it unset so a later sweep retries, mirroring
`submission.classify_and_update`'s "never mass-finalize during an outage" rule. Driven by a
scheduled Celery task (`tasks.run_scheduled_trivia_wiki_incorporation`, hourly, single-flight
cache-locked), same pattern as the generation sweep.

## Solo vs. multiplayer

`TriviaSession`/`TriviaSessionParticipant` were modeled as a proper many-participant session
from the start (not a solo-only shape retrofitted later), exactly like `GameSession` —
every eligibility/scoring rule already reads "all (joined) participants," not "the player."
Solo play creates a single-participant session that's immediately `ACTIVE`; multiplayer creates
a `LOBBY` session that transitions to `ACTIVE` only once the host explicitly begins it — same
lifecycle, same status enum values, as SpotGuessr.

### Multiplayer

- **Inviting**: host-only, friends-only (`services.social.connections.are_connections`), same as
  SpotGuessr — an in-app notification (`NotificationType.TRIVIA_INVITE`) deep-links straight
  into the lobby.
- **Joining**: flips `INVITED` → `JOINED`, idempotent for an already-joined profile, rejected
  once the roster is locked (session no longer `LOBBY`).
- **Starting**: host-only, locks the roster — no one can join after round 1 exists.
- **Real-time sync**: `TriviaSessionConsumer` shares the same `_ParticipantSessionConsumer` base
  class as SpotGuessr's `GameSessionConsumer` (`dashboard/consumers.py`) — connect/disconnect/
  chat-receive/relay-broadcast logic lives once, in one place; each subclass supplies only its
  own `_group_name()`, `_is_participant()`, and `_send_chat_message()`. Route:
  `ws/trivia/session/<int:session_id>/`. Broadcast events: `participant.joined`,
  `session.started`, `answer.submitted` (which profile answered, not their answer or
  correctness — that stays hidden until reveal), `round.revealed`, `round.started`,
  `session.completed`, `chat.message`.
- **Session chat**: `TriviaSessionChatMessage`, plain text, no E2EE — same rationale as
  SpotGuessr's session chat (ephemeral match banter between people already visible to each other
  on the scoreboard, not a private conversation).
- **Answer submission**: `submit_answer` verifies the answering profile is a `JOINED`
  participant of the round's session before scoring (raises otherwise) — an invited-but-never-
  joined profile cannot inflate the answer count used to decide round completion. Uses
  `select_for_update()` on the round row to serialize the race between two participants
  submitting the round-completing answer at nearly the same instant, the same guard
  SpotGuessr's `submit_guess` uses.

### Stall handling and leave/kick (added post-launch, 2026-07-25)

Trivia originally shipped with no way out of a round nobody finishes and no way for a
participant to back out of - or be removed from - a game in progress. Fixed via
`services.trivia.session`, mirroring SpotGuessr's stall-handling shape for the first two and
building genuinely new ground for the third (SpotGuessr has no leave/kick path either yet):

- **`force_reveal_round(round_)`** — the stall-sweep's primitive, identical in shape to
  SpotGuessr's. Reveals a round using whatever answers exist; a participant who never answered
  just scores 0 and isn't rated. Zero answers marks the session `ABANDONED` instead of
  manufacturing an empty next round forever. Driven by `tasks.sweep_stalled_trivia_sessions`
  (Celery beat, every 2 minutes, single-flight cache-locked), force-revealing any session whose
  current round has sat unrevealed past `STALL_ROUND_TIMEOUT_MINUTES` (10) — see
  `TriviaSessionQuerySet.stalled()`.
- **`end_session_now(session, host)`** — the host's manual escape hatch, usable from `LOBBY`
  (cancel before it starts) or `ACTIVE`. Reveals any in-flight round first using whichever
  answers already exist, then always lands on `COMPLETED` — a deliberate host action is never
  treated as abandonment.
- **`leave_session(session, profile)`** / **`kick_participant(session, host, target)`** — a new
  `TriviaSessionParticipantStatus.LEFT` (terminal) status, set when a participant voluntarily
  leaves (or declines an invitation) or the host removes them. Works from either `LOBBY` or
  `ACTIVE`; the host can't kick themselves (use `end_session_now` for that). If the departing
  participant was the last holdout on the session's current in-flight round, removing them can
  complete that round on the spot (reusing the same `_finish_round`/`_advance_or_complete`
  machinery `submit_answer` uses) rather than leaving it stalled until the next sweep. If the
  host leaves, host transfers to the earliest-joined remaining `JOINED` participant; if nobody
  `JOINED` remains, the session is marked `ABANDONED` - the whole table left, the same terminal
  state a zero-answer stall reaches. `invite_to_session` was extended to re-invite (flip back to
  `INVITED`) a profile whose row already exists as `LEFT`, since `get_or_create` alone would
  otherwise silently no-op a re-invite of someone who'd departed. `serialize_session` excludes
  `LEFT` rows from the lobby/roster payload. The UI exposes kick only in the pre-game lobby
  roster (parity with SpotGuessr, which has no live in-round roster either); leave and end-game
  are available from both the lobby and the round view.

### Not built (scope cuts inherited from SpotGuessr's precedent, not oversights)

- **Join-by-link** and **mid-game joining** — same reasoning as SpotGuessr: friends-only invites
  match the site's existing invite model everywhere else, and a fixed roster avoids the
  "does a late joiner replay finished rounds or skip them" problem.
- **A moderation review UI** for AI-flagged (`REJECTED`) questions — an admin/moderator has no
  dedicated screen to review what the classifier rejected and why; the only way to inspect
  `rejection_reason` today is direct DB access. Tracked in `docs/PROBLEMS.md`. Explicitly out of
  scope per product decision (2026-07-25) — not being built.

## Config defaults

| Constant | Default | Note |
|---|---|---|
| `POINTS_FOR_CORRECT_ANSWER` | 1000 | flat, no partial credit |
| `DEFAULT_ROUNDS_PER_SESSION` | 5 | |
| `MIN_ROUNDS_PER_SESSION` / `MAX_ROUNDS_PER_SESSION` | 3 / 20 | |
| `MIN_QUESTION_RATING` / `MAX_QUESTION_RATING` | 1000 / 2000 | difficulty-slider target band |
| `DIFFICULTY_BANDWIDTH` | 200 | Gaussian kernel width, in rating points |
| `MIN_GAMES_FOR_DIFFICULTY_WEIGHTING` | 5 | below this, treat as neutral (1500) |
| `UPVOTE_WEIGHT` / `DOWNVOTE_WEIGHT` / `REPORT_WEIGHT` / `NO_REACTION_WEIGHT` | 1.0 / -1.0 / -3.0 / 0.05 | `services.trivia.voting` |
| `OWN_UNAPPROVED_WEIGHT` | 0.03 | solo-only, "very rarely" surfaced own pending/rejected question |
| `BUILDING_COUNT_QUESTION_THRESHOLD` | 4 | `services.trivia.deterministic` — "more than just a few" |
| `MIN_DESCRIPTION_LENGTH` | 400 | wiki description length before it's mined for AI questions |
| `MAX_QUESTIONS_PER_WIKI` | 3 | per generation sweep, per wiki |
| `WIKI_INCORPORATION_SCORE_THRESHOLD` | 5.0 | well above the `>= 0` rotation gate |
| `DEFAULT_SWEEP_BATCH_SIZE` | 25 | both the generation sweep and the wiki-incorporation sweep |
| Glicko-2: rating / RD / volatility / scale / τ | 1500 / 350 / 0.06 / 173.7178 / 0.5 | Glickman's published defaults, shared code with SpotGuessr |

## Social: ratings visibility

Identical shape to SpotGuessr: a profile always sees its own `PlayerTriviaRating`; a friend's
rating is visible on the overview page only if that friend's `TriviaPreference
.show_ratings_to_friends` is `True` (default **True** — opt-out). `TriviaPreference` also holds
`last_config` (last-used difficulty/rounds settings), same "remember my preferences" role as
`SpotGuessrPreference`.

## SiteSettings moderation toggles

Four independent per-feature AI toggles (`models.site_settings.model.SiteSettings`), each
mapped through `services.ai.factory.get_gateway`'s feature registry:

- `ai_trivia_moderation_enabled` — the shared classifier. When off, user submissions are held
  in `PENDING_REVIEW` indefinitely and AI question generation is skipped entirely; moderation is
  never bypassed by turning this off.
- `ai_trivia_generation_enabled` — AI question mining from wikis.
- `ai_trivia_answer_check_enabled` — the AI fuzzy-answer fallback.
- `ai_trivia_wiki_incorporation_enabled` — the wiki-incorporation writing step.

## Known gaps (tracked in `docs/PROBLEMS.md`, not re-litigated here)

- The `eligible_locations()`/`eligible_questions()` retry-loop pattern (shared with SpotGuessr)
  is inefficient for a sparse eligible pool.
- No moderation review UI for AI-flagged questions — explicitly out of scope per product
  decision (2026-07-25), not being built.
- Thin hypothesis-test coverage on `wiki_incorporation` specifically (the module has ordinary
  tests but no `@given`-based property tests yet), unlike most of the rest of the feature's test
  suite.

## Phase mapping

- **Phase 1 (2026-07-24)** — data model (`PlayerTriviaRating`, `TriviaQuestionRating`,
  `TriviaSession`/`TriviaSessionParticipant`/`TriviaRound`/`TriviaAnswer`,
  `TriviaQuestion`/`TriviaQuestionVote`, `TriviaPreference`); Glicko-2 engine (shared code with
  SpotGuessr); eligibility engine (reusing SpotGuessr's location-eligibility outright);
  deterministic question generation; solo play end to end (start → round → answer → reveal →
  summary); own-rating + friends'-ratings-with-opt-out display; games-hub entry. Models:
  `models/trivia/`. Services: `services/trivia/`. Controller: `controllers/trivia.py`.
- **Phase 2** — multiplayer sessions: friends-only invite/join lobby
  (`TriviaSessionParticipant.status`), `TriviaSessionConsumer` (sharing
  `_ParticipantSessionConsumer` with SpotGuessr) for round sync/live scoreboard, and
  WebSocket-only live text chat (`TriviaSessionChatMessage`).
- **Phase 3** — user-submitted questions (`services.trivia.submission`), the shared content
  classifier (`services.trivia.classifier`) enforcing the person/bullying/in-group/off-topic
  rejection rules, and AI question generation from wiki articles
  (`services.trivia.generation`) — both paths sharing the exact same classifier, per spec. Four
  new `SiteSettings` toggles gating each AI-touching piece independently.
- **Phase 4** — AI wiki incorporation (`services.trivia.wiki_incorporation`): well-upvoted
  user-submitted questions get folded back into their location's wiki article, reusing the
  existing article-expansion/safety pipeline outright.
- **Stall handling and leave/kick (2026-07-25)** — `force_reveal_round`/`end_session_now`
  (mirroring SpotGuessr's post-audit multiplayer hardening) plus a new `leave_session`/
  `kick_participant` path with no SpotGuessr equivalent yet. See "Stall handling and leave/kick"
  above.
- **Follow-up (not yet built)** — a moderation review UI for rejected questions was considered
  and explicitly decided against (2026-07-25); see "Known gaps" above for what remains.
