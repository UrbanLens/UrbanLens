# Hidden reputation points, sensitivity-gated wiki content, and Facts reliability scoring

**Status: DRAFT — needs Jess's input on the open questions in "Decisions needed" before any code
lands.** Written 2026-08-21 from a transcribed voice memo (2026-08-20) plus a codebase survey; see
`ROADMAP.md` UL-397/UL-398/UL-399 for the tracking tickets this design covers.

## The problem

New/unproven accounts can use the wiki system to immediately confirm and explore a sensitive
location the moment they pin it — defeating the "you must already know about a place before we
tell you more about it" principle the whole access model is built on (`docs/ROADMAP.md` §1). The
fix proposed is a hidden, never-user-visible reputation score that scales how much wiki detail a
new account sees, without restricting genuine community members. A second, related ask is
extending the existing Facts reliability system so it can judge a source's trustworthiness by
topic and geography instead of one global number.

## What already exists (read this before designing anything below)

A 2026-08-21 survey found most of the "we should build X" asks in the source voice memo already
have real, working infrastructure. This section is the single most important part of this doc —
skipping it risks reinventing working systems.

- **Consensus points are NOT a usable base for this. Do not extend them.**
  `services/consensus/points.py` looks superficially like the thing this design needs and is not.
  It is a *visible game score*: `ConsensusProfile.total_points`/`level` render straight into the
  Consensus page's HUD (`pages/consensus/index.html:50-54` — `data-points`, `cs-level-badge`,
  `cs-points-value`), and its own module docstring says out-of-game wiki edits are deliberately
  worth less than in-game paths "so playing the game is still the primary way to rack up points."
  That is an engagement mechanic pointed the opposite direction from a hidden anti-gaming score.
  Concretely, five things make it structurally unfit, not just differently-tuned:
  - **Fixed per-action constants** (`SOLO_ANSWER_POINTS = 10`, `MANUAL_EDIT_POINTS = 3`, …) — the
    exact "this action is worth exactly five points" model Jess explicitly ruled out.
  - **No ledger.** `award_points`' `reason` argument is, per its own docstring, "not persisted
    anywhere yet, just surfaced in the log line." Only a running total is stored. That makes four
    separate requirements impossible on this foundation: the admin breakdown by activity type and
    month, per-period diminishing returns (you cannot count this user's comments in January),
    per-activity caps, and retraction of a reverted contribution's points (you cannot subtract
    what you never recorded).
  - **Integer amounts.** The memo's decay curve is explicitly fractional — one point, then half,
    then a quarter.
  - **No awareness of the contribution's target.** The signature is
    `award_points(profile_id, amount, reason)`; it cannot see *what* was contributed to. The
    memo's core mechanic — value derived from how badly the target needed the contribution — is
    not a tuning change to this function, it is a different responsibility.
  - **Synchronous, fire-and-forget at contribution time.** The largest multiplier in the memo is
    *other people later interacting* with your contribution, which is inherently retroactive.

  What IS worth copying from it: the `select_for_update` race-safety pattern for updating a
  denormalized total. That is all.
- **A full activity/streak-tracking system already exists**, independent of points:
  `dashboard/models/achievements/` (`Achievement`, `UserAchievement`, `ProfileActivityDay`,
  `ProfileStreak`) plus `dashboard/services/achievements/` (a `register()`-based metric registry,
  signal-dispatch via a declarative `_SUBSCRIPTIONS` table, Celery-deferred evaluation via
  `transaction.on_commit`, and a nightly `sweep_achievements` safety-net task chunked by profile
  pk range). Metrics already tracked: `pins_created`, `wikis_created`, `photos_uploaded`,
  `places_visited`, `places_rated`, `places_vulnerability_rated`, `places_danger_rated`,
  `friends`, `trips_planned`, `trips_attended`, `wiki_edits`, `comments_written`,
  `markup_maps_created`, `people_invited`, and one `streak_<kind>` per `ActivityKind`
  (`LOGIN`/`PHOTO`/`WIKI_EDIT`/`PIN`/`COMMENT`). No geographic-breadth (pin spread across distant
  regions) metric exists yet — that's genuinely new. The signal-dispatch and nightly-sweep
  patterns here are the template to copy for a reputation aggregator, so it also stays off the
  request path.
- **A community-voted sensitivity rating already exists.** `WikiStatVote`
  (`models/wiki_stat_vote/`) is a 1–5 star vote per `(wiki, profile, field)` for
  `danger`/`vulnerability`/`priority`/`rating`, averaged in `WikiStatVoteQuerySet.composite()` with
  a privacy-fuzzed vote count (reusing the exact same `approximate_pin_count()` fuzz function the
  "fewer than 3 users have pinned this" display uses). **No new rating model is needed** — the
  gating threshold should read `vulnerability`/`danger`'s composite directly.
- **A Facts model with trust-weighted, decaying, conflict-aware confidence already exists** and is
  already feeding the trivia and consensus games. `dashboard/models/facts/` (migration
  `0020_facts`): `Fact` is the resolved `(value, confidence, status)` for one `(subject, key)` pair
  (`subject` = exactly one of Location/Wiki/Image); `FactEvidence` is an append-only observation
  trail with `source_kind`, a static `source_reliability` weight (0.6–1.0,
  `services/facts/evidence.py`), an optional `submitter_trust_snapshot`, and a timestamp.
  `services/facts/confidence.recompute()` combines `source_reliability * trust *
  exp_decay(age, half_life=365d)` and either trust-weighted centroid (numeric/point facts) or
  Bayesian-smoothed agreement clustering (text/choice/bool/date facts) into
  `UNCONFIRMED → TENTATIVE → CONFIRMED`, or `CONTESTED` when the top two clusters are close. A
  `CONFIRMED` value resists flip-flopping on one noisy new observation. **What's missing**: this is
  per-fact, not per-user/per-topic/per-geography. `ConsensusProfile.trust_score` (used as the
  `submitter_trust_snapshot` input) is a single global Beta-Bernoulli scalar per profile — a user
  can't yet be "reliable about trains, unreliable about architecture" or "reliable near NYC,
  unreliable in California." `Fact.key` is a flat unconstrained string with no topic/category
  dimension for cross-location rollups. **This is the actual net-new work**, not the Facts model
  itself.
- **Photo relevant/not-relevant votes are *not* currently trust-weighted**, contrary to what the
  source memo assumed. `MediaRelevance` (`models/images/relevance.py`) is a plain per-profile
  +1/-1 mark, unweighted, blended with SpotGuessr's `GamePhotoFeedback` (thumbs up/down/report,
  180-day decay) into one ranking score in `services/media/media_relevance.effective_relevance()`.
  Separately, SpotGuessr's anonymous photo-coordinate guesses *do* feed the trust-weighted Facts
  pipeline (`record_photo_coordinate_evidence`, fixed 0.6 reliability, no per-user trust since
  anonymous). If per-user trust-weighting photo votes is wanted, that's new wiring, not a fix to
  something broken.
- **Subscription-tier bypass is nearly free.** `SubscriptionRole` is generic and admin-editable
  (not a hardcoded enum); `SiteFeature` (`TextChoices`) enumerates feature flags a role can grant;
  the single `user_has_feature(user, feature)` helper checks site-admin → site-wide default →
  active admin-granted `UserSubscription` → active paid `RoleSubscription`, in that order. Bypassing
  the reputation gate for trusted/paying users is: add one `SiteFeature` value, grant it on the
  `vip` role (seeded via `0020_seed_vip_subscription_role.py`), gate-check with the existing
  helper. No new mechanism needed.
- **The Comment model already has the shape a tenure-gated reply feature needs.**
  `Comment.parent` (self-FK, depth-1 threading, `SET_NULL` + `parent_deleted` flag on parent
  removal) plus `pending_scan` — an existing "hidden from everyone but the author until cleared"
  precedent — is directly reusable for "a new user can see replies to their own comment even
  before they've earned general visibility."
- **Map layers are already first-class.** `CustomLayer` (`models/markup/model.py`) already gives
  one pin/wiki multiple named, ordered, independently-toggleable layers
  (`default_visible`, `PinMarkup.layer` FK). What's missing is any draft/private-until-earned
  visibility concept — no field like that exists on `PinMarkup` or `CustomLayer` today. The closest
  precedent to copy is **`Wiki.officially_created`** (`BooleanField(default=True)`): an
  auto-created draft wiki is treated as "doesn't exist yet" by `get_for_location` and other
  queryset methods until promoted — the same pattern (a boolean the queryset filters on, not a
  bolted-on permission check at every call site) fits a private-until-earned layer.

## The scoring model

The single most important property: **a contribution's value is not a property of the action
type. It is a function of how much the target needed it at the moment it arrived, and of what
happened to it afterwards.** Every design decision below follows from that, and it is why a
constant-per-action table (what Consensus points does) cannot express this system no matter how
carefully the constants are chosen.

An award is roughly:

```
value = base(action_type)              # scarcity of the contribution type itself
      * need(target_state_at_time)     # how badly the target lacked this
      * quality(contribution)          # metadata richness, recency
      * decay(actor, action_type, period)   # diminishing returns
  then, later and separately:
      + amplification(interactions, social_distance)   # retroactive
  all subject to:
      caps(actor, action_type, period, target)
      retraction(if reverted / detected as gaming)
```

**`base` — scarcity of the contribution type.** Rare, high-effort contributions beat cheap
frequent ones. Anchors given: a photo is worth notably more than an alias; upvoting someone
else's photo is worth "a very, very minimal amount." Everything else is calibrated between those.

**`need` — the target's state before the contribution. This is the heart of it.** Worked example
from the memo, for one photo upload:

| Wiki's photo state before | Relative value |
|---|---|
| No photos at all, not even external | Highest — "a significant amount of more points" |
| External photos only, no user-uploaded | High, but below the above |
| Already has many user photos | Low |

And for setting a cover photo: meaningful if the wiki had none; "maybe only worth one point… or
none" if it already had one. The same shape generalizes to every contribution type — a first
link, a first alias, an article where none existed. Computing this requires querying the target's
state at contribution time, which is why the scorer must receive the target, not just an amount.

**`quality` — recency and metadata.** A recent photo on a wiki whose photos are all old is worth
a fair amount (it closes a temporal gap, not just a count gap). Photos carrying GPS coordinates
and capture dates are worth more, "particularly so if it allows us to provide historical
snapshots" — i.e. metadata that makes a photo usable as evidence of what a place looked like at a
known time is the specific thing being rewarded, which ties directly into the temporal-facts work
(UL-403).

**`amplification` — positive interaction from others, weighted by social distance.** The memo's
largest multiplier: contributions others engage with positively are worth "significantly more,"
and engagement from people *not* connected to the contributor is worth more still. This is
necessarily retroactive and asynchronous — points arrive when someone else acts, possibly long
after the contribution. It must be deduplicated (the same person upvoting twice cannot pay twice)
and is the primary collusion surface, since it is the one path where another account's behavior
directly increases your score.

**`decay` — diminishing returns within a period.** Verbatim from the memo: a full point for the
first comment in January, half for the second, a quarter for the third, eventually zero; February
resets to a full point. The intent is to reward *regular, varied* participation over bursts of one
cheap action. Whether the period is calendar-month or a rolling window is open (the memo says
"30 days later" and "in February" in the same breath).

**`caps`.** Per-activity ceilings so no single activity can dominate a total. The memo is
explicitly undecided on the unit — "maybe a lifetime maximum… maybe a per month maximum, or per
time period, per wiki maximum? I'm not entirely sure." Per-wiki caps matter independently of
per-period ones: they bound how much one user can extract from a single target.

**`retraction` and anti-gaming.** A contribution later reverted by someone else should be worth
"none or negative." The named attack: user A edits, friend B reverts, A re-adds — each pass
earning points while the wiki's information content is unchanged. Edit wars generally. The
principle stated: *"a user should really only be gaining points for information that is actually
contributing"* — which is also the stated reason interaction-amplification exists at all, since
information others engage with is the observable proxy for information that was actually worth
having.

**Non-wiki signals, lower weight.** Pin count; geographic breadth (pinning or viewing across New
York *and* California is worth more than a cluster around one's house — a genuinely new metric,
nothing similar exists today); logins and session length; existing achievement stats generally;
friends added; donations (large); invites (reasonable); pin visits; trips created and
participated in; comments and DMs (minimal). These establish "this person actually uses the site"
rather than measuring contribution quality.

### What this implies structurally

1. **An append-only event ledger** — one row per award, recording actor, action type, target,
   timestamp, computed value, and the inputs that produced it. Everything above depends on it:
   the admin breakdown reads it, decay counts it, caps sum it, retraction voids rows in it. The
   stored inputs matter for auditability — a score no one can explain is not tunable.
2. **A scorer registry keyed by action type**, each scorer receiving the target and computing
   need/quality itself, rather than a table of constants. This is where "flexible, not hardcoded"
   becomes concrete, and it mirrors the metric-registry pattern the achievements system already
   uses.
3. **A separate retroactive path** for amplification, triggered by interaction signals, resolving
   the *original contributor* from the interacted-with content and awarding them.
4. **A denormalized total** on the profile for the gate check. The gate is on the request path;
   summing a large ledger per pageview is exactly the performance cost the memo says to avoid.
   Ledger is truth, total is cache, background task reconciles.
5. **Everything off the request path.** Contribution triggers enqueue; scoring runs in Celery.
   This is a bolt-on, and per the memo must never slow the core site down.

## Implementation findings (2026-08-21 deep dive)

Concrete constraints found by reading the code the scorer would have to touch. Several are
blocking — the scoring model above cannot be implemented as described until they're resolved.

### Attribution gaps — and why the obvious fixes are wrong

Three high-value contribution types don't record a contributor on their own row. The obvious fix
for each (add the missing FK / add a `WikiEdit`) turns out to be wrong on closer reading; the
constraint lands on the scorer's design instead.

**Cover photo choice** — `Wiki.cover_photo` (`models/wiki/model.py:173`) is mutated in place by
`WikiCoverPhotoView.post` (`controllers/image_gallery.py:329`) with no `WikiEdit` and no FK. There
is no `Wiki` revision table, so the memo's exact distinction (first cover vs. replacing one) is
unanswerable after the fact.

*Do not fix this by adding a `cover_photo` key to `WikiEdit.changes`.* That dict carries two
behaviors an FK doesn't fit:
- `revert_edit_fields`' fallback branch (`services/wiki/wiki_edits.py:236`) does
  `setattr(wiki, field, old_val)` with the **stringified** stored value, so a revert would assign
  a string to a ForeignKey.
- `wiki_history.html:19-25` renders every key generically as `{{ field }}: {{ diff.from }} →
  {{ diff.to }}`, so a stored pk would surface to users as a bare number.

Supporting it properly would mean special-casing `cover_photo` in the revert path (resolve pk →
`Image`, verify still eligible, skip when deleted) *and* in the history template — real work, for
an audit trail that isn't what the scorer actually needs.

**Instead**: the reputation ledger is the right home for scoring inputs. The scorer snapshots
"was there a cover before" into its own event row. **This forces one architectural constraint:
the cover-photo trigger cannot be a plain `post_save` signal**, since by then the previous value
is gone. Either the view emits the event explicitly with the old value, or the trigger uses
`pre_save`/instance-diffing. Worth settling once and applying to every in-place mutation the
scorer cares about, not just this one.

**Child wiki creation leaves `created_by` null** (`controllers/detail_pins.py:377`) — and it
should stay null. `Wiki.created_by` is not general attribution: its comment says "Used solely to
gate self-service deletion," and `can_be_deleted_by` (`models/wiki/model.py:316`) grants the
creator unilateral delete rights until another profile views the page. Populating it on child
wikis would silently hand out a deletion permission that doesn't exist today. Attribution for
child-wiki creation already exists via the paired `WikiEdit{child_wiki_added}.editor`.

**Wiki boundaries leave `Boundary.profile` null** — also correct as-is. That field is documented
as mirroring `pin.profile` "for fast profile-based queries without joining through pin"
(`models/boundary/model.py:140`), i.e. it means *owner* of a personal boundary, and the
source-candidate unique constraint (`:215`) explicitly requires it to be null. A wiki boundary is
community-editable by many people, so "who drew it" is a history question, not ownership —
overloading `profile` with a second meaning would muddy a field the constraints depend on. The
paired `WikiEdit{boundary_<type>}.editor` is the right trail and already exists.

**Net:** no pre-work migration is needed. Two of the three already have working attribution via
`WikiEdit`; the third is a constraint on how the scorer hooks its trigger.

### Blocking: `Image.profile` is not the author on materialized rows

External media is normally transient (rendered per-request from `LocationCache`), and becomes an
`Image` row only when someone up-votes it or sends it to a wiki
(`services/media/media_materialize.py:195`). On those rows **`profile` is the voter, not the
photographer.** Anything scoring "who contributed this photo" must gate on
`source == ImageSource.UPLOAD` rather than trusting `profile`. The same trap appears in bulk:
`pin.media.send_to_wiki` and the Flickr album import attach many photos *authored by other
people* under the importer's `profile`.

### Answering "how badly was this photo needed?"

No single call exists. It's a two-part query:
- Persisted: `Image.objects.filter(wiki=wiki, media_type=MediaKind.PHOTO)`, split by
  `source == ImageSource.UPLOAD` (user) vs. anything else (materialized external). Note
  `.visible_to(profile)` (`models/images/queryset.py:17`) is eager — narrow before calling it.
- Transient external: requires walking gallery panels —
  `services/pins/external_data.py:1127 get_panel_source(key)`, filter to `GalleryMediaSource`,
  then `LocationCache.get_fresh(location, panel.cache_source)`. Nothing pre-computes this, and it
  hits caches per provider. **This is the one genuinely expensive input in the whole model**, and
  it's needed for exactly one distinction (zero photos vs. external-only). Given the memo's hard
  "must not affect site performance" constraint, this argues for computing need at scoring time
  in Celery — never in the request — and possibly for caching a per-wiki photo-state summary.

### Recency and metadata inputs

- `Image.taken_at` (EXIF `DateTimeOriginal`, `models/images/model.py:249`) is the capture date;
  `created` is upload time. **`taken_at` is frequently null** — no EXIF, stripped uploads, and
  *every* materialized external row (the materialize path never runs EXIF extraction). The
  codebase's standard idiom is `Coalesce("taken_at", "created")`; the scorer should follow it but
  must not treat a coalesced upload date as evidence of capture recency.
- Real GPS is `latitude`/`longitude` being non-null. **Do not use `effective_latitude`** — it
  falls back to the Location's coordinates (`:331`), so it is never null and proves nothing.
- **Privacy interaction that must not become a penalty:** GPS and `exif_data` extraction is
  skipped entirely when the uploader has `track_pin_visits` off (`tasks.py:817-820`). A metadata
  bonus would therefore silently pay users less for having a privacy setting enabled. The bonus
  must be structured as an additive bonus for metadata present, never a penalty for absence, and
  this case deserves an explicit comment and test.
- There is **no field for the date a photo depicts** (vs. when it was taken/uploaded), so a
  scanned historical photo can't declare "this is 1954." The nearest existing logic is
  `services/photos/redata_relevance.py:63`, which diffs `taken_at` against `wiki.date_abandoned`.
  This is a real gap for the "historical snapshot" bonus and overlaps UL-403's temporal work.

### Which interactions can actually feed amplification

| Interaction | Author reachable? | Trap |
|---|---|---|
| `MediaRelevance`, `source == "photos"` | Yes → `Image.profile` | — |
| `MediaRelevance`, external provider | **No** — item is a provider URL hash, `Image.author` is free text, not an FK | Not awardable at all |
| `Reaction` (`models/reactions/model.py`) | Yes, one join per host type | **Positive vs. negative is not modeled** — emoji is a free `CharField`; 👍 and 👎 are indistinguishable. Needs an allowlist. Unique per `(profile, emoji, host)`, so one user with five emoji counts five times |
| `GamePhotoFeedback` | Yes, two joins (`round.image.profile`) | Unique per `(round, profile)` — **repeats across rounds**; must dedupe on `(profile, image)` |
| `TriviaQuestionVote` | Yes, one join | — |

DM and group-message reactions should be excluded — private-conversation signals shouldn't feed a
public-contribution score.

**Reuse precedent:** `services/media/quota_rewards.py` already implements exactly this retroactive
pattern — counting community relevance votes on a photo, excluding the uploader's own vote, and
granting the uploader something for it. The amplification path should follow its shape rather
than invent one.

### Social distance

- `Profile.are_friends(a, b)` (`models/profile/model.py:944`) is one EXISTS query on
  `status=ACCEPTED` only — the cheapest "not a stranger" test, and the right primary predicate.
  Note mute is a per-side boolean, not a status: a muted friend is still a friend.
- For scoring many interactors at once, `Profile.visible_profile_pks(viewer, subjects)` (`:1215`)
  computes common-pin/friend/trip sets for a whole batch in ~3 queries. Use this, not per-pair
  checks in a loop.
- `FriendshipType` already exists (ENCOUNTERED / CONNECTED / FRIEND / CLOSE_FRIEND) — a
  ready-made weighting ladder rather than a binary friend/stranger split.
- `_have_common_friend` (`:1031`) is 4 queries plus a Python intersection and degrades badly with
  hub accounts — acceptable in a Celery task, never in a request.
- `services/pins/common_pins.py` intersects by **place** rather than raw location id, so two
  people pinning opposite ends of one property count as sharing it. That's the better "these two
  aren't really strangers" signal, and its rarity (how many *other* profiles pinned the same
  place) is a natural obscurity weight for the collusion case.

### Revert detection is better than expected

- `WikiEdit.reverted` (bool) and `reverted_by` (self-FK) are set explicitly by
  `revert_wiki_edit` (`services/wiki/wiki_edits.py:244-289`) — no diffing needed for the common
  case. `WikiEditQuerySet.active()` filters `reverted=False`, and
  **`services/achievements/metrics.py:398-407` already excludes reverted edits from the
  `wiki_edits` metric** — an existing precedent for exactly the retract-on-revert behavior wanted.
- Caveats: only the explicit Revert button creates the linkage (a manual re-type is an ordinary
  edit); partial reverts may set nothing; and **revert-of-a-revert clears the flag**
  (`:284-287`), so it is current state, not history — retraction must be re-applicable in both
  directions rather than a one-way subtraction.
- `WikiAutoRemoval` (`models/auto_removals/model.py:83`) is the codebase's existing "this was
  removed, don't let it come back" tombstone, written *before* the delete. Good precedent shape
  for durable retraction.
- Articles use a separate path entirely — **article edits create no `WikiEdit`**, only
  `ArticleRevision`, which has `restored_from` (self-FK) and `editor`. Scoring the highest-value
  contribution type therefore needs its own trigger, not the `WikiEdit` signal.
- **No edit-war or rate limiting exists anywhere** on wiki edits, reverts, or article saves. The
  nearest pattern to imitate is `services/security/email_safety.py:106` (cache reservation plus
  durable log rows over day/month windows).

### Farming surface

Self-limiting by DB constraint (safe to score without caps): stat votes (unique per wiki+field,
max 4), boundary and public-pin votes, wiki creation, aliases and links (unique per wiki), media
votes (unique per profile+location+source+item).

Needs caps or steep decay: Suggest Edits (unlimited re-saves — and note one submit spanning six
fields writes *one* `WikiEdit` with six keys in `changes`, so **score field-count, not row-count**,
and ignore no-op/whitespace flips); comments, markup, custom layers, child wikis (all unbounded
per user per wiki); alias churn (delete-then-re-add writes `WikiEdit` rows both ways). Also:
`Wiki.save()` auto-creates an alias on every rename with `created_by=None` — don't credit those.

**Facts are not a contribution type for scoring purposes.** `Fact` has no submitter FK, and
`FactEvidence.submitter` is written automatically *from* WikiEdits
(`models/wiki_edit/signals.py`), so scoring facts would double-count the wiki edit that produced
them.

### Architecture blueprint (from the achievements system)

The achievements system is the right template, with specific divergences:

- **Copy:** the `_Subscription` declarative table + `_make_handler`/`connect()` generator (with
  the index-in-`dispatch_uid` discipline — Django dedupes on `(dispatch_uid, sender)`, so two
  subscriptions on one model silently clobber each other otherwise); the
  `transaction.on_commit` + `safely_enqueue_task` deferral; the "return early if nothing is
  listening" gate; the dispatch-only chunked nightly sweep with bounded ranges for failure
  isolation.
- **Diverge:** reputation needs `post_delete` as well as `post_save` (retraction); it needs the
  *target owner*, not just the actor (only `_visit_profile_ids` computes a reverse edge today —
  copy that shape); its value callable must take the event/target rather than
  `Callable[[Profile], int]`, and must **snapshot** the computed value into the ledger row rather
  than recompute it later; and the ledger value must be a `DecimalField`, not
  `PositiveIntegerField`, for the fractional decay curve.
- **Don't reuse:** `evaluate_profile`/`_award_qualifying`/`_grant` — built on "count ≥ threshold,
  grant once, never revoke," which is the opposite of an append-only fractional ledger.
- **Do reuse directly as input signals:** `compute_values_bulk(profiles, keys)` and
  `Metric.values_for_many` give per-profile bulk integers with no new queries —
  `pins_created`, `wiki_edits` (already excludes reverted), `photos_uploaded` (already excludes
  non-UPLOAD sources), `places_visited`, `trips_planned`, `comments_written`, `friends`,
  `people_invited`, and `streak_<kind>` (a free tenure/consistency multiplier). Caveat: these are
  *current counts*, not point-in-time — they silently change when a pin is deleted. Fine as a
  multiplier or decay input; wrong as the ledger's source of truth.

## Decisions needed from Jess before building

1. ~~**The REData reputation-scrubbing precedent.**~~ **RESOLVED 2026-08-21 (Jess):** narrowly
   scoped to REData's `GET /photos/reputation/` endpoint (an external ML-quality signal about
   photo *contributions*, not a general policy against ever tracking or displaying individual
   reputation). Does not block this design. An admin-visible per-user point total is fine to build
   — add a comment at that display cross-referencing this doc so a future reader doesn't mistake
   the two for a contradiction.
2. ~~**Where the score lives.**~~ **SETTLED 2026-08-21 by reading the code** (not a judgment call
   after all): a new, wholly separate ledger. Consensus points cannot host this — see the
   inventory above for the five structural reasons. It reads existing signals
   (achievement metrics, activity days) as *inputs* and shares nothing else. Flagged here only so
   a future reader doesn't re-propose merging them.
3. **How much of `need`/`quality` is tunable at runtime vs. in code.** The scorer registry
   (see "The scoring model") settles the "not hardcoded constants" requirement structurally — a
   scorer computes value from the target's state rather than looking up a number. What remains
   open is whether the *coefficients* inside those scorers (how much more a first-photo is worth
   than an Nth, the decay curve's steepness, cap sizes) live in code, in `SiteSettings`, or in an
   admin-editable table. Recommendation: start in code with every coefficient named and in one
   module, ship the admin dashboard, and only promote coefficients to runtime-editable once real
   data shows which ones actually need retuning — premature admin UI for 30 knobs nobody has
   calibrated yet is its own cost. Needs a yes/no.
4. **Gating thresholds and exact scope of "sensitive."** The memo names marker/entrance map detail,
   map markup/custom layers, other users' comments, and the real pinned-user count as *candidates*,
   explicitly not a final/bounded list ("I am not necessarily sure exactly the bounds of what
   constitutes [sensitive] information"). Needs an explicit v1 list before the gating consumer is
   built, sized against `WikiStatVote`'s `vulnerability`/`danger` composite.
5. **Scope of "earn your way in locally.["** The memo describes a real but subtle mechanic: a
   user below the general threshold who makes a first-of-its-kind contribution (first comment on a
   wiki, markup when none exists) can see that specific thread/layer and nothing else. This is
   meaningfully harder than the general point-threshold gate and touches the markup-layer privacy
   field from the inventory above. Consider whether v1 ships without this refinement (general gate
   only) and adds it in a follow-up, versus building both together.
6. **Anti-gaming scope for v1.** Edit-war/collusion detection (voiding points for a wiki edit later
   reverted, or contributions from two colluding accounts alternating an edit back and forth) was
   named as a goal but "an ideal world" aspiration, not a hard requirement. Decide whether v1 ships
   with only the simple case (revert this exact edit → subtract the points it earned) or needs the
   fuller collusion-detection pass before shipping any gating on top of the score.

## Proposed phasing (once the decisions above are made)

Sized to fit the batch-then-adversarial-review working style — each phase is independently
shippable and testable before the next starts.

0. **Attribution gaps first** — nothing above can score cover-photo choice, child-wiki creation,
   or boundary drawing until those record who did it (see "Blocking" above). Small, independently
   useful, and safe to land before any scoring exists: add `WikiEdit{cover_photo}` on cover
   changes, populate `created_by` on child wikis, populate `Boundary.profile` for wiki boundaries.
1. **Reputation ledger + background aggregation.** The model — append-only, `DecimalField` value,
   snapshotted scoring inputs, unique on `(rule_key, actor, target)` for idempotency, plus a
   denormalized per-profile total — the signal-dispatch/nightly-sweep plumbing (copied from
   `achievements/signals.py`, diverging per the blueprint above), and the simplest trigger set.
   Wiki field edits are the natural first trigger since `WikiEdit` already carries editor,
   target, and field-level `changes`. **Write to the new ledger only — leave Consensus points
   entirely alone**; they are a separate visible game score (see the inventory), and the
   double-award/revert inconsistency in them is logged separately in `docs/PROBLEMS.md`
   (2026-08-21). No gating yet; this phase proves the pipeline works and stays off the request
   path.
2. **Remaining trigger types**, each its own small batch: photo uploads (scarcity-weighted per
   decision #3), non-friend-upvote amplification, pin count + geographic breadth (new metric),
   logins/streaks (read from existing `ProfileActivityDay`/`ProfileStreak`), friends, donations/
   subscriptions, invites, trips, comments/DMs (minimal weight), decay/diminishing-returns on
   repeated same-type actions.
3. **Sensitivity-gated wiki content**, reading the ledger from phase 1-2 and `WikiStatVote`'s
   composite (decision #4's v1 list only). Subscription-tier bypass (near-free, see inventory)
   ships in this phase since it's required for the gate to be safe to turn on at all.
4. **"Earn your way in locally"** (decision #5), if scoped for v1 — the `Comment.pending_scan`
   pattern for reply-chain visibility, and the `Wiki.officially_created`-style private-layer field
   for markup.
5. **Admin dashboard**: per-activity/per-month charts, breakdown by trigger type (decision #1's
   resolution determines whether/how an individual's total is ever admin-visible).
6. **Facts topic/geography reliability** (UL-399): decompose `ConsensusProfile.trust_score` into
   per-category Beta-Bernoulli posteriors (mirroring the existing per-mode Glicko-2 pattern
   already used for SpotGuessr/trivia player skill), and add a topic/category dimension to
   `Fact`/`FactEvidence` for cross-location rollups by topic+region. Independent of phases 1-5;
   can run in parallel once someone is free to pick it up.

## Explicitly out of scope for this doc

- Temporal fact-change tracking (fences/cameras/demolition dates as their own timestamped facts,
  reusing the Facts model rather than adding history fields to every model) — tracked separately as
  UL-399's sibling ticket; needs its own short design confirming the exact `FactEvidence` date
  fields already support an optional end date before assuming new schema is needed.
- The floor plan editor UX audit and the sitewide CSS/comment cleanup — unrelated codebase-health
  work, tracked in `ROADMAP.md`'s Code Quality section (UL-400/401/402), not gated on any decision
  above.
