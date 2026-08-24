# Hidden reputation points, sensitivity-gated wiki content, and Facts reliability scoring

**Status: REVISED 2026-08-24 — decisions settled, implementation started.** Written 2026-08-21
from a transcribed voice memo (2026-08-20) plus a codebase survey, then reviewed 2026-08-24
against `docs/PRIVACY_MODEL.md` and the nine access-control fixes that landed with it. That
review found the original gating approach did not close the hole it was written for; see
**Design review** immediately below, which supersedes the later sections wherever they disagree.
See `ROADMAP.md` UL-397/UL-398/UL-399 for the tracking tickets this design covers.

## The problem

New/unproven accounts can use the wiki system to immediately confirm and explore a sensitive
location the moment they pin it — defeating the "you must already know about a place before we
tell you more about it" principle the whole access model is built on (`docs/ROADMAP.md` §1). The
fix proposed is a hidden, never-user-visible reputation score that scales how much wiki detail a
new account sees, without restricting genuine community members. A second, related ask is
extending the existing Facts reliability system so it can judge a source's trustworthiness by
topic and geography instead of one global number.

## Design review (2026-08-24) — read this before the sections below

The design below was written 2026-08-21. Between then and now a privacy sweep closed nine
access-control gaps and produced `docs/PRIVACY_MODEL.md`, which states the access model
explicitly for the first time. Re-reading this design against that document surfaced one
error serious enough to invalidate the original plan, plus a set of smaller corrections.
**Where this section and the sections below disagree, this section wins.**

### R1. The gate as designed does not close the hole it was written for

The threat is: *pin a series of random addresses, see which ones have wikis, and you have
confirmed which are real sites.* The design's answer is to scale **how much wiki detail** a
low-reputation account sees.

That does not work. Detail is not the oracle — **existence is**. A degraded wiki page still
answers "yes, something is here." The probe succeeds in full, and the attacker never has to
read a word of the content they were denied.

Worse, it does not even require opening the wiki. Verified on this branch:

- `templates/dashboard/partials/pins/_pin_detail_hero_body.html:57` branches on
  `{% if pin.community_wiki %}` — a **"Community Wiki"** box with a live link when one
  exists, a **"Create Community Wiki"** button when one does not.
- `Pin.community_wiki` (`models/pin/model.py:738`) resolves through the *place*, so it is
  true for anyone whose pin lands anywhere on the parcel.

Pin an address, look at your own pin page, read the button text. That is the entire attack,
and it costs one page load.

**Correction:** below the threshold, a wiki must resolve as **absent**, not as degraded. The
mechanism already exists and is already the documented contract — `Wiki.officially_created`
makes a draft read as "no wiki exists yet" everywhere, funnelled through
`WikiManager.get_for_location` and `resolve_visible_wiki`, which raises a bare `Http404` for
"no wiki", "draft only", and "not yours to see" alike. The gate belongs in that same
chokepoint, producing the same 404.

### R2. Sizing the gate by vulnerability turns it into a *sensitivity* oracle

The design scales gate strictness by the wiki's community-voted `vulnerability`/`danger`
composite. Combined with a per-place gate, that is strictly worse than the leak it replaces:
pin fifty addresses, and the ones that behave differently are the ones the community has
flagged as sensitive. The gate would rank targets for the attacker.

**Correction — the governing rule for this whole feature:**

> The gate may be visible to the user. It must never vary by place.

A user learning *they* are gated reveals nothing. A user learning *which places* gate them
reveals everything. This resolves the UX tension too: we can be entirely transparent
("community features unlock as you take part") as long as the message is identical
everywhere, and never mentions the place being looked at.

It follows that unrated wikis must fail **closed**. If only rated wikis gate, the absence of
votes is itself the signal.

### R3. Two thresholds, only one of which is per-place

R2 forbids per-place variation; Jess's model calls for vulnerability to influence what is
seen. Both hold, in this order:

1. **`T_community` — site-wide, uniform, not scaled by anything.** Below it, the entire
   community layer is invisible: no wiki existence anywhere, no create affordance, no wiki
   search results, no community photos, comments, markup or floorplans. Because it is
   uniform, it reveals nothing about any particular place. **This is the tier that closes
   the probing attack**, and it is the only one that has to be right for the feature to be
   worth shipping.
2. **`T_detail` — per-wiki, scaled by the vulnerability/danger composite.** Applies only
   *above* `T_community`, where the viewer already knows the wiki exists, so varying it per
   place leaks nothing new. This is where "the content they see is influenced by the wiki's
   vulnerability rating" actually lives.

`T_detail` may start equal to `T_community`, shipping one mechanism and turning the second
on later.

### R4. Prefer a low threshold plus a reveal budget over a high threshold

A high `T_community` punishes genuine newcomers, who are the majority. The attack signature
is not low reputation, it is **volume**: nobody legitimately checks four hundred addresses.

Ship a low `T_community` that ordinary use clears in a session or two, and pair it with a
per-account budget on distinct **new** wiki reveals per window. Copy
`services/security/email_safety.py` wholesale for this — it already does cache reservation
plus durable log rows over hour/day/month windows, with caps resolvable from `SiteSettings`
and overridable per subscription role.

### R5. The gate must be strictly subtractive, and that must be structural

The clear failure mode is reputation quietly becoming a fifth clause of the place-domain
rule — "enough points" granting a wiki the viewer never earned. The gate must only ever
narrow a decision the existing rules already made, never widen one. Enforced by construction
(the gate takes an already-computed result and removes from it) and by a test that asserts no
input to the gate can turn a `False` into a `True`.

### R6. Never gate a user's own content, or the two consent exceptions

`ImageQuerySet.visible_to` ORs `Q(profile=viewer_profile)` before anything else; the gate
belongs strictly inside the *shared* term. The two exceptions ruled on 2026-08-24 —
direct messages, and safety check-ins including signed-out token holders — are consent-based
and must not acquire a reputation check. A signed-out safety contact has no profile and
therefore no score; any gate that reads a score must be unreachable from that path.

On friends: a friend's contribution to a vulnerable wiki is still community content about a
place, and exempting it hands anyone with an established account an unlimited supply of
vouched sock puppets. The better shape is to let being **invited** by an established account
feed the score — bounded by a per-inviter cap so a puppet farm is throttled — rather than
punching a hole in the gate. Content a friend has directed at the viewer *individually* (a
DM, a check-in) is already exempt by R6's first paragraph.

### R7. Retraction must never revoke access already granted

If reverting an edit can drop someone below a threshold, then reverting a rival's edits
becomes a way to lock them out of wikis they were already reading. The score becomes a
weapon.

**Correction:** clearance is durable per `(profile, wiki)` once earned. Losing points stops
*new* reveals; it never takes back an existing one. This must be its own record, **not** a
`PlaceAccessGrant` — that model is clause 4 of the access rule itself, and reusing it would
violate R5.

### R8. Amplification must be weighted by the amplifier's own standing

The design weights interaction by social distance only. A pool of fresh accounts upvoting
each other is then the cheapest attack on the entire system, and it is cheaper than the
attack the system was built to stop. Amplification from a zero-reputation account must be
worth approximately zero.

### R9. "Earn your way in locally", as written, is itself an oracle

The design has a below-threshold user's markup land on a private layer *if markup already
exists*, and grants thread visibility for the *first* comment on a wiki. Both branch on
whether content already existed — so the user learns whether it did.

**Correction: make it unconditional.** A below-threshold user's markup *always* lands on a
private layer; their comment is *always* visible to them and to repliers. Identical
behaviour either way, no oracle — and materially simpler to build than the conditional
version.

### R10. The ledger is itself sensitive data

An append-only table of `(actor, target, time)` is a per-user map of which sensitive places
each person has engaged with — a dataset that does not exist today. The design makes it
admin-visible.

The admin dashboard must default to aggregates. A per-user drill-down that *names targets*
is a new exposure and should not ship in v1. `docs/PRIVACY_MODEL.md` needs a row for it.

### R11. Surfaces this design predates

Written 2026-08-21; the floorplan editor was rebuilt over the following two days.
`Floorplan.wiki` is documented as "visible to everyone who can see that wiki", and a plan now
carries doors, **locks and lock state**, plus `SecurityIndicatorType` on markup already
covering fence / camera / alarm / guard / locked / VPS.

That is the most sensitive payload on a wiki, and it postdates the "what counts as
sensitive" list below. It is also an argument for hooking the gate into **wiki access**
rather than maintaining a per-field list: anything that inherits wiki visibility is covered
the day it is added, without anyone remembering to add it.

### R12. Two live oracles make the gate moot until they are fixed

Found while reviewing, both pre-existing and both independent of this feature:

1. **`SafetyCheckinWikiOptionView` (`controllers/safety.py:1192`, routed at
   `urls.py:1740`) is an unguarded wiki enumerator.** `LoginRequiredMixin` only; it takes
   arbitrary `destination_latitude`/`destination_longitude` from the query string and calls
   `find_community_wiki`, which filters on `wiki__officially_created=True` and **nothing
   else** — no viewer, no `location_visible_to`, no domain check. The template renders the
   wiki's **name**, a **link**, **last-edited time** and **editor count**. Any logged-in
   account can sweep coordinates and enumerate every official wiki in the database. This is
   strictly more powerful than the attack this whole design targets, because it does not even
   require creating a pin.
2. **`officially_created` is enforced per call site, not by the queryset**, and roughly nine
   surfaces omit it — most consequentially `Location.display_name`
   (`models/location/model.py:296`), which reads `self.wiki` unconditionally and so puts the
   *draft's* enrichment-resolved name on every map pin while the pin page still offers
   "Create Community Wiki". `WikiQuerySet` has no `official()` scope; adding one and routing
   through it closes the class rather than the instance.

**Neither is caused by this design, and neither can wait for it.** They ship first.

### R13. The two goals are separable — ship the gate before the fair-scoring system

The need/quality/amplification machinery exists to reward contribution *fairly*. The gate only
needs to answer "is this a real participant, or an account minted ninety seconds ago" — which
a handful of coarse, hard-to-forge signals answer well. Building all of the scorers before any
gating means the privacy hole stays open for the entire build.

Reversed below: ledger with a small signal set, then the gate, then scorer enrichment.

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

## Decisions

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
   *Note (2026-08-24):* `ConsensusProfile`'s own docstring records a decision that its points are
   deliberately **not** a cross-game Profile stat. The new ledger does not reverse that — it is a
   separate table with a separate purpose, and Consensus points stay exactly as they are.
3. **Coefficient tunability.** **RESOLVED 2026-08-24 (Jess): in code.** Every coefficient named
   and gathered in one module; ship the admin dashboard; promote individual coefficients to
   runtime-editable only once real data shows which ones need retuning. Premature admin UI for
   thirty uncalibrated knobs is its own cost.
   *Two carve-outs, on the codebase's own precedent:* the **thresholds** (`T_community`,
   `T_detail`) and the **reveal budget caps** belong in `SiteSettings` from day one, not in code.
   They are operational safety valves, not tuning: if the gate is too tight on launch day it has
   to be loosened without a deploy. This matches `SiteSettings.community_photo_quota_bonus_votes`
   (newer code) rather than `points.py`'s module constants (older).
4. **What counts as "sensitive", and the thresholds.** **PROPOSED 2026-08-24 — for Jess to
   approve or adjust.** Restructured per R1–R3, which changes the shape of the answer: the v1
   question is no longer "which fields do we hide" but "which of two tiers is this".

   **Tier 1 — below `T_community` (site-wide, uniform, no per-place variation).** The community
   layer does not exist for this account:
   - wiki existence itself, everywhere it is currently surfaced: the pin hero's
     Community-Wiki-vs-Create-Wiki branch, `has_wiki` on the detail-pins panel, wiki hits in
     global search and in map autocomplete, `wiki_slug`/`wiki_name` on API photo payloads,
     the safety check-in wiki-notify toggle;
   - the wiki create/claim affordance (uniformly absent, so its absence says nothing);
   - everything reached *through* a wiki: community photos, comments, articles, markup and
     custom layers, floorplans, boundaries, aliases, links, stat votes, owner records.

   Deliberately **not** in tier 1, because none of it is community content and removing it would
   break the product for every new user: the account's own pins, photos, notes and visits; its
   own trips and DMs; safety check-ins; external/provider data about a place (Google, REData,
   weather, imagery), which is public information the site merely relays.

   **Tier 2 — above `T_community`, below `T_detail` (per-wiki, scaled by the
   vulnerability/danger composite).** The wiki is visible and known to exist; what is withheld is
   the material that helps someone *get in*:
   - markup and custom layers carrying `SecurityIndicatorType` (fence, camera, alarm, guard,
     locked, VPS) and entrance/route annotations;
   - floorplans — interior geometry, doors, and lock state (R11);
   - other users' comments;
   - the true pinned-user count, degraded to the existing `approximate_pin_count` fuzz rather
     than to a different-looking placeholder;
   - community photo **captions and EXIF-derived capture metadata** (the photos themselves stay;
     it is the "when and exactly where" that is the access hint).

   **Starting thresholds.** `T_community` low enough that a genuine first session clears it, with
   the reveal budget (R4) carrying the real anti-probing load. `T_detail` at the composite's
   midpoint, so only wikis the community has actively flagged apply it. Both in `SiteSettings`
   per decision 3, both to be re-tuned from the admin dashboard's real distribution once there
   is one.

5. **Scope of "earn your way in locally".** **RESOLVED 2026-08-24 (Jess): build both.** Shipped
   with R9's correction — the local path is **unconditional**, never branching on whether content
   already exists, because that branch is itself an oracle. A below-threshold account's markup
   always lands on a private layer; its comment is always visible to itself and to anyone who
   replies. Precedents to copy: `Comment.pending_scan` for reply-chain visibility,
   `Wiki.officially_created` for the private-layer boolean the queryset filters on.
6. **Anti-gaming scope for v1.** **PROPOSED 2026-08-24.** Ship the simple case plus the one
   structural defence, defer the detective work:
   - **In v1:** retraction on explicit revert (`WikiEdit.reverted` already exists and
     `services/achievements/metrics.py` already excludes reverted edits — the precedent is
     written); re-applicable in both directions, since a revert-of-a-revert clears the flag;
     amplification weighted by the amplifier's own standing (R8), which removes the sock-puppet
     ring structurally rather than by detection; per-actor and per-target caps.
   - **Deferred:** edit-war and collusion *detection*. It is an "ideal world" goal, it is the
     part most likely to produce false positives against real users, and R7 means a wrong verdict
     can no longer lock anyone out of a wiki they already read — which is what made getting it
     right urgent in the first place.

## Phasing (revised 2026-08-24)

Reordered per R12 and R13: close the live holes first, then build the smallest thing that
shuts the probing attack, then enrich. Each phase is independently shippable and testable.

0. **Close the two live oracles (R12).** Independent of everything below, and the gate is
   pointless while either stands.
   - Gate `SafetyCheckinWikiOptionView` on `location_visible_to`.
   - Add `WikiQuerySet.official()` and route the surfaces that omit `officially_created`
     through it — `Location.display_name` first.
1. **Attribution gaps** — unchanged from the original plan, and still genuinely small. The
   scorer cannot attribute cover-photo choice without the ledger snapshotting the previous
   value at trigger time, which is a constraint on the trigger, not a migration.
2. **The ledger + background aggregation.** Append-only, `DecimalField` value, snapshotted
   inputs, an idempotency key (see below), and a denormalized per-profile total. Signal
   dispatch and the nightly sweep copied from `models/achievements/signals.py`. A **small**
   initial signal set — the coarse, hard-to-forge ones the gate actually needs: account
   tenure, distinct activity days, non-reverted wiki edits, genuine uploads, pins created,
   accepted invitations. No gating yet; this phase proves the pipeline stays off the request
   path.
3. **The gate (`T_community`) + the reveal budget.** The tier that closes the attack. Hooked
   into `resolve_visible_wiki` / `get_for_location` / `visible_wiki_location_ids_cached` —
   the same authorities the 2026-08-24 sweep consolidated on, never a parallel filter.
   Subscription bypass ships here (one `SiteFeature` value, `user_has_feature`), because the
   gate is not safe to turn on without it.
4. **`T_detail`** — the per-wiki, vulnerability-scaled tier, and the field list from decision 4.
   Needs a denormalized composite on `Wiki`: it is computed per call today, at 8 queries per
   wiki page, and the gate cannot afford that on the request path.
5. **"Earn your way in locally"** (decision 5), unconditional per R9.
6. **Scorer enrichment** — need/quality/amplification, decay, the remaining trigger types.
   This is where the original design's substance lives; it improves *fairness*, and by this
   point the privacy hole is already shut.
7. **Admin dashboard** — aggregates by activity type and period. Per-user drill-down that
   names targets is deliberately out of v1 (R10).
8. **Facts topic/geography reliability** (UL-399) — independent of 0–7, can run in parallel.

### Two constraints on phase 2 that came out of the survey

**Idempotency is the highest-risk gap.** `CELERY_TASK_ACKS_LATE` plus
`autoretry_for=(OSError,)` means every task can be redelivered and re-run. Achievements are
protected by `UniqueConstraint(profile, achievement)`; an append-only ledger has no natural
equivalent, so one must be designed in from the start — a unique tuple over
`(actor, rule_key, target_type, target_id, period)`.

**Ledger writes are synchronous; only derived work defers.** The governing precedent is
`_record_streak_days` (`models/achievements/signals.py:180`), which is deliberately *not*
deferred because streaks are the only metric with no source of truth outside our own tables.
A ledger is exactly that. `safely_enqueue_task` swallows broker failures and returns `None`,
so a deferred ledger write is silently lossy. The row is written inside the contributor's
transaction — where a rolled-back contribution rolls its row back too — and only recomputing
the denormalized total and re-evaluating gates goes to Celery.

## Explicitly out of scope for this doc

- Temporal fact-change tracking (fences/cameras/demolition dates as their own timestamped facts,
  reusing the Facts model rather than adding history fields to every model) — tracked separately as
  UL-399's sibling ticket; needs its own short design confirming the exact `FactEvidence` date
  fields already support an optional end date before assuming new schema is needed.
- The floor plan editor UX audit and the sitewide CSS/comment cleanup — unrelated codebase-health
  work, tracked in `ROADMAP.md`'s Code Quality section (UL-400/401/402), not gated on any decision
  above.
