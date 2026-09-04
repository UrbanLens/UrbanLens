# GOALS.md vs. code and tests: audit

Generated 2026-08-24, updated 2026-08-25, by reading the actual implementation and actual test
suite for each `docs/GOALS.md` topic — not by reading other docs (that pass is
`docs/audits/GOALS_AUDIT.md`). Each entry below records the defect (or confirms the match) with
file:line evidence, so it does not have to be re-derived. All thirteen topics from the first
audit round completed as of 2026-08-25 (pin-to-wiki-sharing initially failed structured-output
validation and was re-run). A second round is auditing six topics/sub-claims the first round
never touched — three entire `GOALS.md` sections with zero prior coverage (self-hosting and
federation; testing/CI quality; i18n and observability) plus three sub-claims buried inside an
already-audited section (map UI consistency; trip photo archive encryption, distinct from backup
encryption; whether "pins in common"/aggregate-count features compare at the Place/Wiki level as
required, or at the Pin/Location level) — found by re-reading `docs/GOALS.md` section-by-section
against the first round's topic list rather than assuming thirteen topics meant full coverage.
Several findings below have since been fixed — see [Fixes applied](#fixes-applied-2026-0824-25),
which is the authoritative status; the per-topic sections underneath describe what was found
*before* those fixes, kept as evidence rather than rewritten.

**How to read status:** `CONTRADICTS` = the code actively does the opposite of the goal, today,
on `main`/this branch. `PARTIAL` = part matches, part doesn't, or the mechanism exists but an
architectural choice differs from the goal's literal wording. `GAP` = nothing built.

## Summary

| Topic | Status | Headline issue |
|---|---|---|
| [Safety check-ins](#safety-check-ins) | ~~CONTRADICTS~~ **fixed** | Token portal showed the full plan/location/photos to anyone holding the link, before an incident — no `escalated_at` check |
| [Trip activities](#trip-activities-sourcing) | ~~CONTRADICTS~~ **fixed** | Sharer's live private pin name/coords leaked via the external API, both map endpoints, and the Memories→Sharing page — all four fixed |
| [DM/group-chat encryption](#dm-group-chat-encryption) | CONTRADICTS | Plaintext body is a fully supported, unenforced path everywhere (server, WS, external API) — same conclusion as `docs/audits/GOALS_AUDIT.md` #1, now code-confirmed |
| [Pin privacy default](#pin-privacy-default) | ~~PARTIAL~~ **fixed** | Ownership scoping was solid everywhere except the pin-share detail page, which read the sender's live `pin.description` (private notes) regardless of share status — fixed |
| [Pin-to-pin sharing](#pin-to-pin-sharing) | PARTIAL | Photo "copy" is still a storage-key alias, reference-counted at delete time rather than actually copied |
| [Pin-to-wiki sharing](#pin-to-wiki-sharing) | ~~PARTIAL~~ **partly fixed** | Field/alias copying is solid; shared photos are the *same row* re-pointed at the wiki, not copied — deleting it from either side now unlinks instead of destroying (fixed); repositioning it from either side still mutates what the other side shows (open) |
| [Wiki access](#wiki-access) | ~~PARTIAL~~ **partly fixed** | Core access rule is excellent; two search surfaces used a narrower ad-hoc filter (fixed); "concealed users" is real but stranded on a far-diverged release branch |
| [Discovery: browse/search parity](#discovery-browse-and-search-parity) | ~~PARTIAL~~ **partly fixed** | Same ad-hoc-filter pattern hit 4 more search providers (fixed); Routes/Lists/SavedFilters/Albums still have no search entry point |
| [Lists: filter/manual reconciliation](#lists-filter-manual-reconciliation) | PARTIAL | Manual **add** persists correctly; manual **remove** does not — a resync silently re-adds it |
| [Public pins](#public-pins) | PARTIAL | Vote engine is faithfully strict; "visible without earned wiki access" is not actually wired to anything — open design question |
| [Backups and logs](#backups-and-logs) | ~~PARTIAL~~ **partly fixed** | Backup encryption: zero code (open). Log rotation: `app`/`app-ws`/`nginx` stdout now bounded (fixed); daphne/nginx access-log files still unbounded |
| [Games](#games) | PARTIAL | SpotGuessr/Consensus feed the general Facts schema correctly; Trivia bypasses it entirely; anonymization-on-delete is unverified by any test |
| [Saved filter performance](#saved-filter-performance) | ~~PARTIAL~~ **partly fixed** | Real self-invalidating cache exists; the fingerprint query is now memoized per-request (fixed); the post-toggle map render still has no cache |

### Round 2 — topics the first round never touched

Each finding below was independently adversarially verified when it was `CONTRADICTS` or `GAP`
(a second agent tried to refute it by re-reading the cited code itself); every one CONFIRMED.
`PARTIAL` findings were not auto-verified by the workflow, so the cross-pin-aggregate finding
below was checked by hand before acting on it.

| Topic | Status | Headline issue |
|---|---|---|
| [Self-hosting and federation](#self-hosting-and-federation) | PARTIAL | No Kubernetes requirement genuinely holds; "single-user desktop-style, not an afterthought" does not — one 9-service compose stack for every deployment size, `app` hard-`depends_on` Valkey, ClamAV fails closed by default |
| [Testing and CI/CD quality](#testing-and-ci-cd-quality) | ~~PARTIAL~~ **partly fixed** | Success/failure-path and Hypothesis-additive practice are strong (sampled); DB-collision is worked around not root-caused (open); two structural pre-commit checks had no CI backstop (fixed) |
| [i18n and observability](#i18n-and-observability) | GAP | Zero translation infrastructure in active use (`LocaleMiddleware` absent, no `{% trans %}` anywhere); zero uptime-monitor/alerting integration of any kind |
| [Map UI consistency](#map-ui-consistency) | ~~PARTIAL~~ **partly fixed** | Shared toolbar/layers system is real and used almost everywhere; `memories/locations.html`'s bespoke select-toggle button (fixed, mirrors the already-fixed `visits.html` sibling); two boundary-drawing maps and the floorplan editor still bypass it (open) |
| [Trip photo archive encryption](#trip-photo-archive-encryption) | GAP | No trip-photo-archive concept exists in the data model at all (only `Image.pin`/`.wiki`/etc, no `Image.trip`/`.trip_activity`); no photo bytes of any kind are encrypted at rest anywhere in the app |
| [Cross-pin aggregate comparison level](#cross-pin-aggregate-comparison-level) | ~~PARTIAL~~ **fixed** | `common_pins.py` already compares at the Place level correctly; four other call sites (`Profile._have_common_pin` and its two batch twins, `wiki_community_summary`, trip-activity `COMMON_PIN` visibility) compared raw `Location` rows instead — all four fixed to reuse the same place-aware pattern |

---

## Safety check-ins

**Goal:** contacts get no live location by default, and get the trip plan only after a failed
check-in ("incident"), via token link, with no site membership required.

**What's real:** live location is genuinely gated by construction — `consumers.py:666` only
joins the location broadcast group for a session connection (`self.contact is None`), never a
token connection, and there's no REST path into location data for a contact at all
(`external_api/views_safety_location.py`, gated by `SafetyCheckinViewerScopedView`). Well
tested, including the exact edge case (a contact who is *also* an accepted partner still gets
nothing over the contact socket).

**What's broken:** `SafetyContactPortalView.get` (`controllers/safety.py:1505-1537`) and
`SafetyContactMarkupJsonView` (`controllers/markup.py:228-255`) resolve access purely by
`SafetyCheckinContact.objects.by_token(token)`, then unconditionally render `plan_details`,
`contact_message`, route markup, and photos — **no check on `escalated_at`/`notified_at`/status
anywhere**. The only thing keeping this pre-incident-safe is that the token is never emailed to
the contact until `escalate_checkin` fires. That's disclosure timing, not access control: an
unauthenticated GET to a leaked, guessed, logged, or forwarded token returns the full plan
regardless of check-in state. The codebase already implements the correct pattern one view over
— `_render_community_view`/`community_status.html` (`controllers/safety.py:717-768`) branches on
`escalated_at`/`is_resolved` and its own docstring says "plan, contact message, emergency
contacts, photos, and chat stay owner-only." The token portal, which is the channel the goal
explicitly names, never applies that same check.

**Tests:** zero coverage of the actual portal route in any state. Two tests confirm the *token
itself* never leaks through other APIs, but nothing exercises what the token *grants*.

**Fixed this round** — see [Fixes applied](#fixes-applied-this-round-2026-08-24).

Also flagged, not yet acted on:
- `invite_checkin_partner` (`services/visits/safety.py:666-716`) requires an existing `User` by
  username, and `SafetyCheckinPartner.profile` is non-null — the "opt a contact into enhanced
  access" tier structurally requires the invitee to already be a site member, in tension with "a
  contact never needs to be a site member" if that's meant to cover both tiers. **Ask Jess**:
  is the two-tier model (token contact: no membership, post-incident, plan-only; partner:
  membership + accept, pre-incident, full access) the intended design? If so the goal doc/CLAUDE.md
  should say so explicitly.
- `mark_found_safe`'s notification loop to `other_contacts` is unfiltered by `notified_at`,
  unlike `notify_contacts_of_update` which filters to `notified_at__isnull=False` — worth
  checking whether resolving a check-in can hand a portal token to a contact who was never
  escalated to.

---

## Trip activities sourcing

**Goal:** trip activities source display data from the Wiki (or a consented copy), never a live
pin; `record_share_exposure` tracking is correct and should be kept, but per Jess's own
clarification (`docs/audits/GOALS_AUDIT.md` #3) it should reference a Place/Boundary/Wiki, not a
private pin.

**What's broken, concretely, in three places:**

1. `TripActivitySerializer.effective_title` (`external_api/serializers.py:2834`) sources
   `activity.effective_title` directly — the *unmasked* model property, which falls back
   `title → pin.display_label → location.display_name`. The internal HTMX panel already has a
   fix for exactly this leak class (`_masked_activity_title`/`display_title`,
   `services/trips/trip_activities.py:339-357`, with a comment describing the prior leak it
   patched) — the external/mobile API serializer just never got routed through it.
2. `build_trip_map_points` (`services/trips/trip_map.py:70,96`) sets `label = act.effective_title`
   — also unmasked, and shared byte-for-byte by both the web map and the mobile map endpoint.
3. `_record_detected_trip_share` (`services/trips/trip_share_tracking.py:93-103`) calls
   `PinShare.objects.create(pin=pin, ...)` — storing the sharer's actual Pin FK on a share the
   module's own docstring says is "never actionable, never materializes a Pin." That reference
   isn't inert: `PinShareQuerySet.received_by()` has no status/origin filter
   (`models/pin_share/queryset.py:148-157`), so these DETECTED shares surface on the recipient's
   Memories→Sharing page like a real share, and `sharing.html:130` renders
   `{{ group.pin.effective_name }}` — the sharer's live private pin name, unconditionally.

`activity_coords()` (`services/trips/trip_legs.py:41-56`) has the same problem for
coordinates — it prefers the pin's *current* location over the activity's own stored snapshot,
so moving the sharer's pin silently drags every trip member's map marker with it. This is a
direct instance of the goal's own litmus test ("if editing a pin's fields ever causes a
different user's view to change, that's a bug").

**Tests:** the share-bookkeeping mechanism (`test_trip_share_tracking.py`) is solidly tested.
Zero tests touch what gets *displayed* — every `location_hidden` fixture in the trip test files
sets an explicit `title=`, which trivially bypasses the pin-fallback path the bug lives in, so
the existing suite structurally cannot catch this.

**Recommended actions** (not yet made — these are three separate, moderately-sized changes, and
the actual "Wiki-first" redesign `effective_title`'s docstring already claims to do doesn't
exist yet):
- Route `TripActivitySerializer.effective_title` and `build_trip_map_points`'s label through the
  same masking `trip_activities.py` already uses internally.
- Stop rendering `group.pin.effective_name` for DETECTED/TRIP_ACTIVITY-origin shares on the
  Sharing page — exclude non-actionable shares from `incoming_share_groups`, or source the label
  from something other than the live pin.
- **Ask Jess**: does `LocationExposure`/`PinShare` keying off `Location` (not the newer `Place`
  model) satisfy the "Place, Boundary, or Wiki" wording from the audit clarification?

---

## DM/group-chat encryption

**Goal:** E2EE "as close to unconditionally enforced as possible — no way to turn it off."

**Confirms `docs/audits/GOALS_AUDIT.md` #1 at the code level, precisely.** The key-management
infrastructure itself is excellent (X25519 identity keys, Argon2id wrapping, passkey/PRF unlock,
per-conversation/per-group keys, auto-enrollment on every password login and silently for OAuth).
But `DirectMessage.body` (`models/direct_messages/model.py:34`) is a real, non-deprecated
plaintext field; the DB constraint (`db_dm_body_xor_ciphertext`) only enforces *mutual exclusion*
with `ciphertext`, not that `ciphertext` is ever required. `create_direct_message`
(`services/messaging/direct_messages.py:685-876`) has zero requirement that either party be
E2EE-enrolled. Every real entry point — WebSocket consumer, the no-JS HTML fallback, and the
external/mobile API — can create a fully plaintext DM with no gate. `GroupMessage` mirrors the
identical structure. The frontend client library documents plaintext fallback as *deliberate*
(when a partner isn't enrolled, or the device is locked) — a state the server cannot currently
distinguish from "deliberately plaintext."

**What does match:** self-destructing messages are a real, well-tested hard-delete —
`hard_delete_expired_direct_messages` issues an actual row `DELETE` (ciphertext included), not a
soft-hide, and cleans up attached images too.

**Tests:** the crypto plumbing (enrollment, unlock, wrong-key rejection, race conditions,
enumeration-oracle prevention) is exceptionally well tested. But no test anywhere asserts the
actual goal — that a plaintext send is refused. Every existing test treats plaintext-body
creation as an ordinary, accepted path, because the code does too.

**Not acted on — this is a real design decision, not a bug to silently fix.** Confirm with Jess:
was "no way to turn it off" meant as "the client defaults to encrypting, no UI toggle to disable
it" (already true) or "the server refuses to ever persist a plaintext message" (not true today,
and would break the locked-device and unenrolled-partner cases the client currently handles
gracefully)? If the stricter reading is correct, the lowest-disruption first step is a
per-profile "require encrypted messages" opt-in that `create_direct_message` enforces, rather
than an unconditional block.

---

## Pin privacy default

**Goal:** a pin is private by default; every endpoint enforces this by construction; the
litmus test is that editing a pin never changes another user's already-rendered view.

**What matches, thoroughly:** every ownership-scoped read/write path checked — internal REST
(`PinViewSet.get_queryset`), external API (`OwnedPinMixin._owned_pins`), cover-photo mounting,
wiki-relink — enforces owner-only access and answers a non-owner's request with 404 (never 403,
so existence is never confirmed). Games source displayed content from Wiki/Location, never Pin.
Pin-share *acceptance* (`create_pin_from_share`) is a genuine field-by-field copy onto a new,
independently-owned row, and there's a structural test (`test_share_pin_copy_fidelity.py`, via
AST inspection) that a new `Pin` field can't silently skip the copy.

**What's broken:** `PinShareDetailView` (`controllers/pin_sharing.py:236-247`) puts
`share.pin` — the **sender's live Pin instance** — directly into the template context, and
`pin_share/detail.html` reads `pin.description` and `pin.address_basic` straight off it
(plus `child_share.pin.display_label` for bundled children). This page is reachable both while
PENDING and indefinitely after ACCEPTED/REJECTED. If the sender edits their pin's description or
address at any point — before the recipient ever opens the link, or years after they accepted —
the recipient's view changes to match, because it's reading the live row. `shared_name` exists
specifically to snapshot the *name* for this exact reason (its docstring documents the
live-name behavior as deliberate) — there's no equivalent for description or address.

**Tests:** the ownership-scoping half has both success and failure tests everywhere.
`test_pin_share_detail_view.py` only asserts who can load the page (200/404) — nothing asserts
which fields are shown or where they come from, so this leak is invisible to the suite.

**Recommended fix** (not yet made — touches the share model, two creation call sites, a
controller, and a template; queued for a future round): add `PinShare.shared_description` (and
`shared_address` if the page should keep showing one) snapshotted at share-creation time,
mirroring `shared_name`; stop passing `share.pin` into the template for text fields; fix the
bundled-children list the same way. **Ask Jess** whether `shared_name`'s existing live-name
behavior should also become a snapshot now that the privacy goal is stated this explicitly, so
the fix doesn't quietly change that too.

---

## Pin-to-pin sharing

Matches my own direct investigation earlier this round almost exactly, with more detail. The
share/accept lifecycle (friends-only gating, row-locked no-op-replay on concurrent accept,
independent Pin row with real field-by-field copy) is solid and well tested.

**The photo-copy bug is still architecturally present**, just no longer symptomatic:
`create_pin_from_share` builds the recipient's `Image` rows with `image=image.image.name`
(`services/sharing/pin_sharing.py:225`) — the sender's storage key, not new bytes. Nothing
duplicates the file. `delete_stored_file` (`services/media/images.py:726-760`) now
reference-counts before deleting from storage, and is correctly wired into all 8 real deletion
call sites — so the *symptom* Jess reported (sender deletes → recipient's photo breaks) is fixed.
But the two rows still alias one file on disk, which is not what "copied to the recipient" means
literally, and provenance ("who it came from") is only reconstructable informally via storage-key
string matching, not a tracked field.

**Tests actively encode the alias as correct, not as a bug:**
`test_share_pin_copy_fidelity.py:105` asserts `copied.cover_photo.image.name ==
"photos/cover.jpg"` — the *same* key as the source. `test_shared_image_file_deletion.py` is the
one file that names the aliasing explicitly and tests the reference-count mitigation, but by its
own premise (`test_the_copy_reuses_the_same_stored_file`) it documents the shared-file design,
not an independent copy. **If the storage architecture ever changes to true copying, both of
these tests need inverting — nothing today would flag that they're testing the old behavior.**
No test covers: byte-level independence (mutating the sender's file post-share), a caller that
bypasses `delete_stored_file`, in-place file overwrites, or multiple simultaneous recipients
(the goal's own "every recipient" wording).

**Not acted on — real architecture tradeoff, ask Jess:** true per-recipient file duplication
(matches "copied" literally, costs storage) vs. keeping reference-counted dedup by storage key
(cheaper, already shipped, but is sharing-by-reference not sharing-by-copy) vs. dedup-by-content-hash
using the existing `checksum` field (a middle ground). Whichever is chosen, add an explicit
`Image.copied_from` provenance FK rather than reconstructing lineage from a filename string
match, and add the byte-independence and multi-recipient tests either way.

---

## Pin-to-wiki sharing

**Goal:** sender opts in per field; the wiki gets a copy; the wiki never has read access to the
source pin or any data inside it. (Added 2026-08-25 — the initial audit agent for this topic
failed structured-output validation and was re-run.)

**What matches, and matches well:** the actual implementation lives in
`services/wiki/wiki_creation.py` + `models/pin/signals.py` + `models/aliases/signals.py` +
`models/wiki_stat_vote/` — **not** `services/sharing/`, which is entirely pin-to-pin provenance
and contains no wiki code at all. Two real mechanisms both genuinely copy: `WikiCreationService
.create_for_pin()` lets the pin owner explicitly pick which fields/aliases/photos to hand over
(`include_fields`/`alias_ids`/`image_ids`), seeding each into a brand-new `WikiStatVote` row —
`Wiki` has no FK to `Pin` anywhere in the model, so "no read access to the source pin" holds
structurally. Ongoing per-field auto-sync (`sync_pin_stats_to_wiki`, `sync_rating_to_wiki`,
alias sync) mirrors changes into the same independent `WikiStatVote`/`WikiAlias` rows, each
gated by its own `Profile.sync_*` opt-in setting. Pin's own `name`/`description` (personal notes)
are never seedable at all. Well tested: every sync toggle has both a positive and a
disabled/negative test, plus an adversarial test that a tampered POST can't re-enable sync while
`community_enabled=False`.

**What's broken: shared photos are the same row, not a copy — and it's worse than the
pin-to-pin case.** `WikiCreationService._seed_photos` does
`pin.images.filter(pk__in=image_ids).update(wiki=wiki)` — it repoints the *existing* `Image`
row's `wiki_id`, leaving `pin_id` unchanged (an `Image` row carries independent nullable `pin`
and `wiki` FKs). This is confirmed deliberate, not an oversight — the test itself
(`test_wiki_creation.py::test_photos_only_seeded_when_chosen`) asserts the row is "still attached
to the original pin too." The consequence: `PinImageView.post`/`delete`
(`controllers/image_gallery.py:267-294`) let the pin owner reposition or hard-delete that exact
row from the *pin* gallery after it's been shared to a wiki — a pin-side action silently mutates
or destroys what wiki viewers see. Unlike the pin-to-pin sharing case, **there is no
`delete_stored_file`-style reference-count protection here at all** — deleting unconditionally
removes the row, full stop, since it's the one row both sides point at. This is a direct instance
of the same "never a live reference" violation flagged under
[Pin-to-pin sharing](#pin-to-pin-sharing), on a different surface, with a thinner safety net.

Also noted: `SEEDABLE_FIELDS` intentionally excludes security indicators (fences/alarms/etc — a
`SECURITY_FIELDS` constant is defined but never referenced again in the file) — worth confirming
that's deliberate rather than a missed field, since danger/vulnerability are seedable but the
related security indicators aren't.

**Tests:** strong for the field/alias copy mechanism (see above). Zero coverage of the actual
"copy, not live reference" invariant for *any* field type — no test asserts a `WikiStatVote`/
`WikiAlias` row survives deletion of its source Pin/Review, and no test exercises the photo
cross-mutation case (deleting or repositioning a shared photo from the pin side and checking what
the wiki side shows afterward) at all.

**Not acted on — same architecture question as the pin-to-pin photo case, ask Jess together:**
whichever resolution is chosen for [Pin-to-pin sharing](#pin-to-pin-sharing)'s photo-copy
question should extend here too — but note this side currently has *zero* protection (not even
the reference-counted mitigation the pin-to-pin case has), so if true copying isn't chosen
immediately, this specific gap (an unprotected shared row) is the more urgent half to patch with
at least a `delete_stored_file`-equivalent guard in the meantime.

---

## Wiki access

**The core rule is excellent and thoroughly tested.** `wiki_access._domains_given_pins` is the
single implementation (domain-based via `Place.domain_root_id` + `PlaceAccessGrant`, never reads
the user-drawn `Boundary` table — the anti-gaming invariant). `resolve_visible_wiki` raises a
bare `Http404` identically for all four "can't see it" causes, and every wiki-scoped surface
(14+ controllers, 14 external-API call sites) routes through it. `external_api/errors.py`
additionally collapses every 404/403/etc. under wiki views to a byte-identical body specifically
to defeat a slug oracle. Test coverage here is unusually strong: hypothesis property tests over
building/member counts, ~4 separate anti-gaming tests using an adversarially huge polygon, and
`test_external_api_wiki_oracle.py` loops 23 routes × 3 "not found" causes asserting
byte-identical response bodies.

**Two real, contained inconsistencies** (under- not over-inclusive — no access leak, but they
violate "enforce by construction everywhere"): `services/map_pins/autocomplete.py`'s wiki-search
block and `global_search/providers.py`'s `WikiSearchProvider` both filter by exact `Location`
match instead of calling the shared `wiki_access.location_visible_to`/`visible_wiki_location_ids`
— the same function 24 other files already use correctly (e.g.
`services/custom_fields/custom_field_references.py`). Net effect: a user who's genuinely earned a
wiki via a boundary-mate pin won't find it by searching, only by browsing directly to it. (Same
root cause surfaces again, worse, under [Discovery](#discovery-browse-and-search-parity).)

**"Concealed users" is real, just not here.** `grep -rli concealed src/urbanlens` finds nothing
outside the CLAUDE.md files that quote the goal. But `git log --all --oneline -i
--grep=conceal` returns ~60 commits building exactly this feature — field-granular provenance,
per-viewer resolution, several adversarial-hardening passes — all living only on
`origin/@release/v_0_7_0`. That branch and current `HEAD` have diverged 34/199 commits each way;
`main` is 958 commits behind it entirely. So this is a real, already-built feature stranded on a
branch, needing a deliberate merge/port decision — not a "go build it" task. Worth noting: that
branch's own history records its own adversarial-review failures (e.g. "six of fourteen
concealment assertions did not test what they named") — a port should re-review critically, not
merge wholesale on the assumption that "exists on another branch" means "correct."

**Ask Jess:** what should happen to the concealment branch (merge/rebase/cherry-pick), and is
`global_search.WikiSearchProvider`'s narrower-than-the-page-rule behavior intentional (search
relevance) or a bug?

---

## Discovery: browse and search parity

Same root cause as wiki-access's search gap, but wider: `WikiSearchProvider`,
`ArticleSearchProvider`, `CommentSearchProvider`, and `PhotoSearchProvider` (all in
`global_search/providers.py`) each independently reimplement wiki access with the narrow exact-
`Location` filter instead of calling `wiki_access.location_visible_to`. `MarkupMapSearchProvider`
is worse — it scopes by the *annotation's own creator*, never checking wiki access at all, even
though wiki-scoped markup is documented as "shared community data... visible to any signed-in
user with wiki access" (`models/markup/model.py:390-395`). None of these are access leaks (all
are under-inclusive relative to real access), but they're a systemic violation of "find anything
by searching alone."

**Separately, four entire browsable entity types have no search provider at all:** Routes,
`PinList` ("lists," browsable at `/lists/`), `SavedFilter` (browsable via the map sidebar), and
`Album`. None appear in `providers.py`'s `default_providers()` or `parser.py`'s keyword table.

**Tests:** `MarkupMapSearchProvider` and `SafetySearchProvider` have zero test coverage of any
kind — not even a basic "finds by title" case. The four wiki-adjacent providers only get
API-scope-gating tests using exact-Location fixtures; the boundary-mate scenario
(`test_wiki_access_boundary_mates.py` already proves the real rule) is never run against any of
them, so the under-coverage bug is completely unguarded.

**Recommended actions** (queued, not yet made): fix the four providers to call
`wiki_access.visible_wiki_location_ids`/`location_visible_to` (a same-file reuse of an
already-canonical function — the single highest-value fix in this topic); fix
`MarkupMapSearchProvider` to scope by wiki/pin access, not annotation creator; add search
providers for `PinList`/`SavedFilter` names; **ask Jess** whether Routes/Albums are deliberately
search-exempt or a gap.

---

## Lists: filter/manual reconciliation

**Manual add is solid**: `sync_pin_against_smart_lists`/`resync_smart_list` never duplicate (a DB
`UniqueConstraint` backs this up) and never auto-remove an `ADDED_MANUAL` row. Well tested.

**Manual remove is not durable — this contradicts the goal, not just the documented UI gap.**
`remove_pins_from_list` just deletes the `PinListItem` row; there's no exclusion/tombstone state
(`ADDED_VIA_CHOICES` only has manual/smart_filter/boundary). Its own docstring admits "a later
resync may of course re-add it if it still matches" — so any subsequent pin save, label change,
or filter edit silently recreates a row the user explicitly removed. "Manual decisions persist
even as filter membership changes" only holds for adds today.

**Related gap:** `add_pins_to_list` silently no-ops when a pin is already present via
smart_filter/boundary match, without upgrading `added_via` — so explicitly re-adding an
already-matched pin doesn't make the decision durable; it stays exposed to later auto-removal.

**A separate, unrelated bug found in the same file:** `filter_matching_ids`/`_pin_matches_filter`
omit `.root_pins()`, unlike every saved-filter preview call site — this is the
`docs/PROBLEMS.md:585` `root_pins()` bug, confirmed still present, but it's a candidate-matching
defect, not the reconciliation issue above.

**Tests:** no test ever constructs the goal's exact scenario (a pin present via both filter and
manual add), and no test exercises manual removal followed by any resync trigger — which is
exactly the path that would currently fail and prove the contradiction.

**Not acted on** — needs a data-model decision (an exclusion state, or a separate table) that's
better made by Jess than guessed at. **Ask Jess**: was "manual decisions persist" meant to cover
removal too (as GOALS.md's wording suggests) or only addition? If removal-persistence is in
scope, the "known gap" in GOALS.md should be recharacterized — it's a data-model gap, not just a
missing UI affordance.

---

## Public pins

**The vote engine matches "deliberately very strict" faithfully**: simultaneous thresholds
(aliases, links, photos, article length, markup, vulnerability score, pinner floor scaled to
community size), a 15km exclusion radius against already-passed locations, 75% consensus with a
7-day minimum open window, and a permanent hard-fail at 10 ballots/75% no.

**"Visible without earned wiki access" isn't actually wired to anything.**
`resolve_visible_wiki` — the sole gate for the wiki page and the vote endpoint — has zero
awareness of `PublicPinCandidateStatus.PASSED`. A non-pinning profile still gets an
indistinguishable 404 on a PASSED location's wiki page. The only thing that becomes newly
reachable is an opt-out `PinSuggestion` queue entry and a separate demo-instance bulk export —
and there's a dedicated, already-passing test (`test_demo_public_location_export.py`) whose own
comment states "having a wiki is not being public - wiki access is earned per viewer," suggesting
this was a deliberate choice. There's also dead code consistent with an earlier, different intent:
`public_vote_context` explicitly checks `PASSED` before pin ownership (built to serve a
non-pinning viewer), but its only two callers are both unreachable by one, since
`resolve_visible_wiki` 404s them first.

**Ask Jess directly:** does "public pin" mean the live wiki page becomes viewable by any
logged-in profile once a location passes, or is today's behavior (surfaces only via the opt-out
suggestion queue and demo export, wiki page itself stays gated) the intended design? This is a
real ambiguity in the goal's wording, and the code has (unreachable) traces of having tried to
support the more permissive reading at some point.

**Also worth a sanity check at current scale:** `pinner_floor_min=2`/`min_votes_to_pass=2` means
a location can pass with the minimum possible two users — easy to trigger at the project's
current ~2-user beta scale, even though the formula is designed to tighten automatically as the
user base grows.

---

## Backups and logs

**Backup encryption: total gap, matches `docs/audits/GOALS_AUDIT.md` #4 exactly.**
`DatabaseBackup.run()` shells out to plain `pg_dump ... -f temp_path` — no encryption step
anywhere. Retention/purge (30-cycle, oldest-first) is real and tested. `docs/DATA_ENCRYPTION.md`
already documents this gap in prose and lists it as open Follow-up #6.

**A second, more specific gap the goal calls out directly:** self-destructing messages are
supposed to be excluded from backups "entirely, even encrypted." But `DatabaseBackup.run()` does
a full, unfiltered `pg_dump` with no table/row exclusion — a message still live at backup time
lands in that day's dump verbatim, and nothing ever scrubs an already-written backup after the
live row is later hard-deleted. It just ages out over ≤30 days of retention rotation.

**Log rotation: partial.** Django's `RotatingFileHandler` (10MB × 5 files) is real, but
size-triggered, not interval-triggered — a low-traffic deployment could keep a stale log for
months without ever rotating. At the container level, `db`/`celery-worker(-panels)`/`celery-beat`/
`clamav`/`valkey` all have a bounded `logging: {driver: json-file, max-size/max-file}` block in
`docker-compose.yml` — but **`app`, `app-ws`, and `nginx`, the three services that actually log
client IPs, have none.** Worse, two IP-carrying access logs bypass Docker logging entirely: nginx's
own `access_log` is deliberately routed to `/tmp/nginx-requests.log` (off the stdout default) with
no rotation, and daphne's `--access-log` writes unbounded, forever, to the *persistent* logs
volume. `docs/ROADMAP.md` (UL-136) already tracks this as open.

**Tests:** the backup mechanics that exist (scheduling, retention, stale-temp reaping) are well
tested with a Hypothesis property test. Nothing tests encryption (none exists) or the
backup/hard-delete interaction. Zero test coverage for log rotation anywhere.

**Not acted on this round** — backup encryption needs a real key-management design decision
(where does the decryption key live?) before it's a one-line fix; the backup/self-destruct
interaction needs a design decision on scope (accept a bounded exposure window, or build active
post-backup scrubbing). **The docker-compose logging blocks for `app`/`app-ws`/`nginx` are a
purely mechanical, low-risk fix** (matching the existing pattern used for the other 5 services) —
queued for a near-term round.

---

## Games

**Wiki-not-Pin sourcing matches cleanly across all three games** and is well tested with
explicit success/failure pairs and the documented single-user exceptions (SpotGuessr Photos
mode's `solo_profile` branch; Consensus has no exception at all — `ConsensusRound.wiki` is a
mandatory FK).

**The general-purpose Facts schema (`Fact`/`FactEvidence`) is genuinely subject-, key-, and
type-agnostic**, and SpotGuessr/Consensus both write into it correctly (trust-weighted for
Consensus). **Trivia bypasses it entirely** — a well-upvoted question gets AI-paraphrased and
committed *directly into the wiki article body*, with no confidence value or evidence trail. AI-
generated and deterministic Trivia questions (including `built_year`, a *registered* Fact key)
never produce evidence at all — `built_year` has zero producers anywhere, i.e. a dead registry
entry.

**Anonymize/forget on account deletion is structurally correct but has zero test coverage.**
`FactEvidence.submitter` is `SET_NULL`; `Guess`/`ConsensusAnswer`/`TriviaAnswer` cascade-delete
with the account. Coherent design, but `test_account_deletion.py` never touches any of these
models — the "anonymize/forget" half of the games goal is completely unverified.

**Not acted on — needs a product decision, not a code guess: ask Jess** whether Trivia is
intentionally exempt from the Facts schema (a deliberate simpler pipeline) or should route
through `services.facts.evidence.record_evidence` like the other two games. Either wire a real
producer for `built_year` or remove the dead registry entry. Queue an account-deletion
integration test (`FactEvidence`/`Guess`/`ConsensusAnswer`/`TriviaAnswer`/`TriviaQuestion`
survive-vs-cascade) for a near-term round — this one has no design ambiguity, it's just missing.

---

## Saved filter performance

**A real, deliberately-designed two-layer architecture exists**: an instant client-side
optimistic filter pass over already-rendered markers, backed by a self-invalidating Redis cache
(`get_or_compute_matching_uuids`, keyed by a fingerprint of the profile's pins plus the filter's
own `updated` timestamp — no separate invalidation signal needed).

**Two real gaps undercut "not a perceptible round trip":**
1. The pin fingerprint (a DB aggregate) is recomputed **once per active filter**, and — worse —
   **once per every saved filter the profile owns** in `SavedFilterMatchCountsView`, which fires
   on every toggle. A profile with N saved filters pays N redundant identical queries per toggle
   instead of one.
2. The *authoritative* post-toggle map re-render (`MapPinPayloadService.all`) has no caching
   layer at all — distinct from the separate `MapPinCache` used only for plain viewport loading.
   Every toggle re-queries and re-serializes the full matching-pin set from scratch; it's masked
   by the client-side optimistic pass, not eliminated.

**Tests:** cache-invalidation correctness is well tested. Zero use of
`assertNumQueries`/`django_assert_max_num_queries` anywhere in the saved-filter test files, and
zero entries in the project's own perf-fingerprint tool (`test_query_records.perf.yml`,
`docs/TOOLING.md`) for any saved-filter/toolbar endpoint — so this is exactly the kind of
regression that tool exists to catch, and isn't catching here. One explicit security claim in
`_apply_toolbar_filters`'s own docstring (a foreign-profile's filter id is silently dropped, no
leak) is also untested.

**Not acted on this round** — the fingerprint-memoization fix (compute once per request, pass it
in) is small and mechanical; queued for a near-term round along with the `assertNumQueries`
regression test and the untested foreign-filter-id security claim.

---

## Self-hosting and federation

**Goal:** a non-community, no-Kubernetes deployment must work fine as a single-user desktop-style
setup, not an afterthought. (Aspirational, lower priority: move toward federation/P2P — okay if
never reached.)

The "no Kubernetes" half is genuinely satisfied — no K3s/CNPG/Garage/WireGuard references exist
anywhere in this repo (that infrastructure work lives entirely in `../infrastructure`). The
"single-user desktop-style, not an afterthought" half does not hold up: there is exactly one
documented deployment path (`docker compose up --build`), and it stands up the identical
9-container stack (app, app-ws, nginx, db, celery-worker ×2, celery-beat, clamav, valkey)
regardless of scale — no lightweight profile exists. The Django settings layer *does* have real
graceful-degradation intent for a Valkey-less deployment (`CACHES`/`SESSION_ENGINE`/
`CHANNEL_LAYERS` are conditionally configured, `channel_broadcast.send_group_message` no-ops when
there's no channel layer, `safely_enqueue_task` catches a broker-unreachable error rather than
raising), but that intent is undercut in three places: `docker-compose.yml`'s `app` service hard
`depends_on: valkey: condition: service_healthy` (Compose won't even start without it); the
no-config `CELERY_BROKER_URL` fallback is a hardcoded `redis://localhost:6379/0`, not an
in-process transport, so a from-scratch install with nothing configured silently loses every
background feature (backups, panels, exports, WS push) rather than "working fine"; and ClamAV
fails **closed** by default (uploads rejected with 503 if unreachable), with the opt-out
documented only in a Pydantic field description, not README or any self-hosting guide.
Federation/P2P is a clean, appropriately-unbuilt GAP — the goal itself says that's fine, and the
one related design doc (`docs/designs/rejected-and-deferred/split-architecture.md`) is explicitly
deferred and explicitly excludes federation/P2P from its own scope.

**Tests:** none target "self_hosted"/"standalone"/"single_user" deployment modes at all. What
exists tests the individual mechanisms in isolation with both a success and failure path
(`test_malware_scan.py`'s ClamAV-disabled vs. unreachable cases; `test_infrastructure_stats.py`'s
missing-Valkey/missing-broker "disabled" reporting) — nothing boots the app or exercises a real
request (login, create a pin) with no Valkey/Celery reachable to confirm the user-visible
experience actually degrades gracefully rather than silently losing functionality.

**Not acted on — real product-scoping question, ask Jess:** does "single-user desktop-style setup"
mean a dedicated lightweight compose profile (drop valkey/celery-worker-panels/celery-beat/clamav/
app-ws), or does "not required" already satisfy the goal with "desktop-style" read as softer
framing? See [Open questions](#open-questions-for-jess).

---

## Testing and CI/CD quality

**Goal:** every test asserts both success and failure paths; Hypothesis is additive to unit tests,
not a replacement; every page has at least one unit + one integration test; DB-collision-under-
parallelism gets investigated as a setup/teardown problem, not just worked around; pre-commit/
release-please friction gets fixed, not worked around.

A meta-audit of the suite's own health, sampled rather than exhaustive (785 files live under
`dashboard/tests/hypothesis/`; ~10 were read in full plus every systemic sweep test found by
targeted grep). Findings differ sharply by clause:

- **Success+failure path, and "every page" coverage:** largely MATCHES, structurally rather than
  by per-feature diligence — `test_cross_user_route_access.py`/`test_write_route_smoke.py` iterate
  every registered Django route and assert refusal/no-500 as one property covering all current and
  future routes; the integration suite's `pages.spec.ts` discovers its page list from the rendered
  nav at runtime, so a new page is automatically swept.
- **Hypothesis additive, not replacement:** MATCHES in every file sampled — hand-written example
  tests sit alongside a separate `@given`-decorated property test for the same behavior, never
  instead of it. Only ~19% of the directory actually uses `@given` at all (the directory name is
  legacy/generic).
- **DB-collision "investigated as setup/teardown problem first":** CONTRADICTS as practiced.
  `docs/TOOLING.md` and `bin/run_tests.sh` both document the same conclusion — xdist parallelism
  "multiplies concurrent load on Postgres, which is exactly what has been observed to take the
  local instance down" — and the response was to make `--parallel` opt-in, not to root-cause the
  crash mechanism. (This matches the project's own `local-postgres-crashes-under-concurrent-load`
  memory note — "retry with a fresh `UL_TEST_DB_NAME`" is a detection/retry heuristic, not a fix.)
- **Pre-commit/release-please friction "fixed, not worked around":** PARTIAL, with a real
  precedent already established — `check_imports_tracked.py`/`check_migration_graph.py`/
  `check_doc_line_refs.py` were pre-commit-only and got added directly to `ci.yml` (2026-08-11
  audit), exactly the right fix. Two more structural hooks, `check_outage_not_cached.py` and
  `check_notification_choke_point.py`, were left behind with no CI backstop — **fixed this
  round, see Fixes applied #12.** The already-known release-branch CI gap (`ci-only-runs-on-main-
  prs` memory) remains open — still a `pull_request: branches: [main]`-only trigger, worked
  around via manual `gh workflow run --ref`, not fixed.
- Mutation testing (`[tool.mutmut]`) is scoped to exactly 3 files, a documented, reasonable cost
  tradeoff — but it means "prove the failure path is actually caught" is machine-verified for well
  under 1% of the codebase.

**Not acted on — needs Jess:** whether to widen the release-branch CI trigger (an infra decision
with real cost/behavior implications, not a one-line mechanical fix like the two check scripts
were); whether the DB-collision investigation should go further (a controlled test against an
isolated Postgres instance to separate "shared dev box" from "the app's own setup/teardown spikes
connections"); whether mutation-testing scope should grow to cover safety check-ins/e2ee.

---

## i18n and observability

**Goal:** i18n/translation is a near-term goal (the site is US/English-centric, team relocating to
Europe). Recurring uptime-monitor alerts get investigated, not tuned out.

Both sub-goals are a clean, verified GAP. **i18n:** `LocaleMiddleware` is absent from `MIDDLEWARE`;
`LANGUAGE_CODE` is hardcoded `en-us`; no `LOCALE_PATHS`, no `locale/` directory, no `.po`/`.mo`
files anywhere in the project. Exactly 2 templates load `{% load i18n %}`, both solely to stamp
`<html lang="...">` — zero `{% trans %}`/`{% blocktrans %}` tags exist anywhere in the template
tree, so every visible string (nav, buttons, dialogs, toasts) is hardcoded English. Only 3 Python
files touch `django.utils.translation` at all, all backend-only validation/choice-label strings.
Independently corroborated two ways: `docs/PROBLEMS.md` explains the *only* two `{% load i18n %}`
usages were added for an accessibility fix (`html-has-lang`), not a translation effort; and
`docs/audits/GOALS_AUDIT.md`'s separate, doc-based audit pass reached the identical conclusion. **Uptime
monitoring:** no alerting/monitoring integration of any kind exists (`health.py` is unauthenticated
liveness/readiness probes only, not alerting). A real internal Gotify admin-alert channel exists
but is wired to exactly one event (`PIN_IMPORT_ERROR`), explicitly not-yet-generalized per
`docs/ROADMAP.md`'s own open ticket — reinforcing rather than contradicting the GAP.

**Tests:** none — there is nothing built for either sub-goal to test yet.

**Not acted on — needs Jess:** is i18n meant to be scoped for this beta (per project memory, ~2
users) or does it genuinely block the Europe relocation, in which case it needs real scoping
(LocaleMiddleware + LANGUAGES + LOCALE_PATHS + wrapping the many hardcoded strings + a real
compiled `.po`/`.mo` proving the pipeline works)? Does uptime-monitoring belong in this repo at
all, or does it live in `../infrastructure`/a third-party dashboard already (per project memory)
and the goal is already satisfied elsewhere, just undocumented as such?

---

## Map UI consistency

**Goal:** every map on the site has the same controls, layout, and behavior where they apply; a
specialized map's unique controls still stay consistent with the rest.

A real, well-used shared component system exists (`{% map_toolbar %}`/`{% map_layers_panel %}`,
`MAP_TOOL_REGISTRY`/`MAP_LAYER_REGISTRY` in `templatetags/map_components.py`, bound at runtime by
`window.MapLayers`) and covers the large majority of map surfaces: the main map, pin-share/trip/
pin-list/saved-filter/common-pins/Memories/wiki/pin-detail maps, and the safety check-in map (with
a documented readonly exception). Four deviations found:

1. `memories/locations.html` still rendered a hand-rolled `.pin-select-toggle` button instead of
   the registered tool + `{% map_toolbar %}` — **the identical defect already fixed on the sibling
   `memories/visits.html` page**, whose own regression test says "replaced with the same
   `{% map_toolbar %}` component every other map uses," just never applied to this second page.
   **Fixed this round, see Fixes applied #10.**
2. Two fully-interactive boundary/region-drawing maps (`pin_lists/detail.html`'s smart-list
   boundary editor, `_saved_filter_dialog_scripts.html`'s region editor) bypass the shared layer
   system entirely — raw `L.tileLayer(OSM)`, no layers panel, no dark mode, no toolbar. Not fixed
   this round — a real (if probably uncontroversial) UI migration, not a one-line swap.
3. The floorplan editor renders its own bespoke toolbar that visually mirrors the main map's
   placement (per its own comment) without actually reusing `MAP_TOOL_REGISTRY`.
4. Consensus/SpotGuessr's game-round maps omit `{% map_toolbar %}` and use a non-standard
   layers-panel wrapper/position — plausibly intentional for the compact embedded game shell, but
   undocumented as such (unlike the safety check-in's documented exception).

**Tests:** narrow and template-tag-scoped — the tag renderers are tested in isolation, but only
one page-level regression guard exists (`test_memories_unlogged.py`, for `visits.html` alone,
which is exactly why the identical defect on `locations.html` survived undetected). No test
enumerates every map-bearing template and asserts each conforms or is an explicit exception.

**Not acted on — needs Jess for items 2-4:** are the boundary/region-drawing maps and the
floorplan editor intentional, permanent exceptions worth documenting (like the safety check-in
readonly case), or technical debt worth migrating onto the shared system? Is the games' non-
standard toolbar treatment deliberate for the embedded shell, or drift?

---

## Trip photo archive encryption

**Goal:** trip photo archives, especially for completed trips, should be encrypted, ideally
end-to-end. (Distinct from the separate, already-audited backup-encryption goal.)

A clean, independently-verified GAP on both axes. **No trip-photo-archive concept exists in the
data model at all**: none of `Trip`/`TripActivity`/`TripMembership`/`TripComment`/
`TripActivityRSVP`/`TripActivityVote` has an FK to `Image`, and `Image` has FKs to pin/wiki/
location/safety_checkin/visit/direct_message/pin_suggestion but nothing trip-shaped. The only
file field near trips is `TripComment.image`, a single per-comment chat attachment — not a
gallery, and `trips/detail.html` has no photo/gallery section at all. A trip activity links to a
`Pin`, and any photos live on that pin's ordinary gallery with zero trip-specific handling.
**Nothing encrypts photo bytes anywhere in the app, trip or otherwise**: `Image.image` is a plain
`ImageField` on default `FileSystemStorage`; `docs/DATA_ENCRYPTION.md` explicitly excludes pins/
locations/trips/wikis from its scope and states plainly that the media volume has "None at rest";
the E2EE layer is scoped to DM/group bodies and safety archives only. "Completed" trip status
changes nothing about photo handling — no signal, service, or management command connects trip
completion to encryption, archival, or access reduction of any kind.

**Tests:** none, because nothing exists to test — every trip-related test file was grepped for
image/photo/encrypt with zero matches.

**Not acted on — needs Jess, this is a scoping question before it's an implementation one:**
should "trip photo archive" be a first-class linkage at all (an `Image.trip_activity` FK or a
trip-level gallery), or was the goal always "photos on pins that happen to be part of a trip"? If
trip photos should be genuinely end-to-end encrypted, that's a different mechanism entirely from
`EncryptedTextField` (server-side Fernet, text-only) — closer in shape to the E2EE message layer,
needing per-trip-member key sharing designed from scratch. Given beta scale (~2 users per project
memory) and the goal's own "ideally" framing, recommend scoping down to server-side at-rest
encryption or access-reduction on completed trips as a first step, pending confirmation.

---

## Cross-pin aggregate comparison level

**Goal:** aggregate cross-pin stats ("N users have this pinned") and "pins in common" style
features must compare at the Place or Wiki level, never by comparing pins/Locations directly.

UrbanLens's place model is a three-tier chain (`Pin` → `Location` → `Place`), and many `Location`
rows can legitimately share one `Place` (the parcel/building) — `Location.objects
.get_nearby_or_create`'s own docstring states the architecture explicitly: "consolidating two
drops at one real place is the *place's* job... they share its wiki, its community, and its
'places in common' entry." One implementation already gets this right:
`services.pins.common_pins._pinned_keys` (now public, `pinned_place_keys`) keys each pin by
`("place", place_id)` when known, falling back to `("location", location_id)` — its own docstring
records the motivating bug it fixed: "two friends who explored the same property and pinned it
fifty metres apart used to show zero places in common." **The same bug, left unfixed, was found
in four other places that never got that fix:**

1. `services.wiki.community_counts.wiki_community_summary` (the literal "N users have this
   pinned" feature) counted root pins on only the single `Location` the caller resolved the wiki
   through — `resolve_visible_wiki` deliberately allows that to differ from `wiki.location` so
   long as they share a Place, so the count varied by which of a place's several pinned
   coordinates the viewer's URL happened to name, and structurally undercounted.
2. `Profile._have_common_pin` — backs the `COMMON_PIN`/`ANYTHING_IN_COMMON` profile-visibility
   gates, an **access-control** decision (whether a stranger can see another profile's identity),
   not just a display number. Did a raw `location_id` set intersection with no Place fallback.
3. `Profile.visible_profile_pks`/`viewers_who_can_see` — the batch equivalents of #2,
   reimplementing the same raw comparison rather than delegating to the fix.
4. `services.trips.trip_visibility.apply_trip_visibility_filter`'s `COMMON_PIN` branch — same raw
   `location_id` comparison gating whether a trip activity's location is shown to a viewer.

**Fixed this round** (see Fixes applied #11): `pinned_place_keys` made public and reused directly
by #2; #3 and #4 reimplement the same place-with-location-fallback key logic in batch-safe form
(they can't call the single-pair helper without reintroducing the N+1 queries they exist to
avoid). `WikiStatVote.composite()` was already immune (it filters by `wiki` directly, never
`Location`), and needed no change.

**Tests:** before this round, every test file touching this area constructed its "shared" fixture
with exactly one `Location` row both profiles' pins pointed at directly — never two distinct
`Location` rows sharing one `Place` — so the place-aware branch of `pinned_place_keys` itself was
never exercised by anything except `common_pins.py`'s own tests, and #2-#4's bug was invisible to
the existing suite by construction. New tests added this round for all four fixed call sites (see
Fixes applied #11) close that gap.

**Not acted on — needs Jess:** whether to go further and extract one single shared helper all four
(five, including `common_pins.py`) call sites route through, versus the current state (one
canonical `pinned_place_keys` reused directly by #2, the same logic duplicated in batch-safe form
at #3/#4) — given how many independent copies of this bug already existed, consolidation looks
likely right, but that's an API-surface decision, not a bug fix.

---

## Fixes applied (2026-08-24/25)

Every fix below is a case where the code contradicted the goal with no real design ambiguity —
either it mirrors a correct pattern already established elsewhere in the same codebase, or it's
a straightforward mechanical change (config, a missing queryset method, a redundant query). Each
has new tests; test coverage status is noted per item — "confirmed" means the actual pytest run
finished and passed, not just that the code was written and syntax-checked.

1. **Safety check-in token portal: pre-escalation plan/location leak.**
   `SafetyContactPortalView`/`SafetyContactMarkupJsonView` rendered the full trip plan, contact
   message, route, and photos to *any* holder of a valid contact token, regardless of whether the
   check-in had escalated. Fixed by mirroring `_render_community_view`'s existing
   `checkin.escalated_at` gate. 8 new tests in `test_safety.py`. **Confirmed: 8/8 passed.**
2. **Trip-activity title/coordinate masking gaps.** `TripActivitySerializer.effective_title`
   (external API) read the raw, unmasked `activity.effective_title` instead of the row's
   already-masked `display_title` — fixed by pointing its `source` at the row key instead.
   `build_trip_map_points`'s child-trip ghost markers checked only `location_hidden`, missing the
   per-adder `trip_pin_location_visibility` check the parent trip's own activities already get —
   fixed by applying `viewer_hidden_activity_ids` there too. 4 new tests across
   `test_external_api_trips.py` and `test_trip_child_trip.py`. **Confirmed: 68/68 passed**
   (includes the existing `TripMapParityTests`, so the web/mobile map-payload parity guarantee
   still holds).
3. **Docker log rotation for `app`/`app-ws`/`nginx`.** Added the same bounded
   `logging: {driver: json-file, max-size/max-file}` block the other 5 services already had.
   YAML-validated; this doesn't need a pytest run. Does **not** cover daphne's/nginx's own
   *access-log files* (see open questions below — that needs a real rotation mechanism, not a
   compose edit).
4. **Search-provider domain-access gaps.** `WikiSearchProvider`, `ArticleSearchProvider`,
   `CommentSearchProvider`, `PhotoSearchProvider` (`global_search/providers.py`) and
   `services/map_pins/autocomplete.py`'s wiki search each used an ad-hoc exact-`Location` filter
   instead of the canonical `wiki_access.visible_wiki_location_ids` — fixed by switching all five
   to the canonical function (also added the missing `officially_created=True` filter to
   autocomplete). New file `test_search_wiki_domain_access.py` (13 tests) plus the existing
   `test_wiki_access_boundary_mates.py`/`test_global_search_engine.py` suites as regression
   guards. **Confirmed: all passed** (13/13 new tests + no regressions in the ~50 existing tests
   re-run alongside them).
5. **`pin_list_membership.py` missing `.root_pins()`.** Confirmed the still-live
   `docs/PROBLEMS.md:585` bug: `_pin_matches_filter`/`filter_matching_ids` omitted `.root_pins()`,
   unlike every saved-filter preview call site, letting a child/detail pin enter smart-list
   membership. Fixed by adding the same call. New `FilterMatchingIdsExcludesChildPinsTests` class.
   **Confirmed: 19/19 passed** (full file, no regressions).
6. **Saved-filter fingerprint memoization.** `get_or_compute_matching_uuids` recomputed the
   profile's pin fingerprint (a DB aggregate) on every call — once per active filter in
   `_apply_toolbar_filters`, once per *every* saved filter the profile owns in
   `SavedFilterMatchCountsView`, on every single toolbar toggle. Fixed by computing it once per
   request and threading it through (`pins_fingerprint`, now public, takes an optional
   `fingerprint=` param). New test asserts the fingerprint function is called exactly once per
   request regardless of filter count. **Confirmed: 18/18 passed.**
7. **Memories→Sharing page leaked the sharer's live pin for DETECTED-status shares.** The
   received-shares view (`controllers/memories.py`'s `incoming_share_groups`) read
   `share.pin`/`share.place_label` unconditionally, including for `PinShareStatus.DETECTED`
   shares (auto-recorded from a shared map, a DM, or a trip activity — "never actionable,
   never materialize a Pin" per the status's own docstring). Unlike an `EXPLICIT` share
   awaiting accept/reject, where showing a preview of the current pin name is the point, a
   DETECTED share is something the recipient never consented to see anything about at all —
   this was a live reference violating the goal's own litmus test, and applied to *every*
   detection origin (map/DM/trip-activity), not only trip activities as originally scoped.
   Fixed with a new `_safe_incoming_place_label` helper: a group falls back to the snapshotted
   `Location` label instead of the live pin whenever *every* share in it is DETECTED-status; a
   mixed group (DETECTED plus a real pending/accepted share) still shows the pin, since a
   legitimate share justifies it there. 5 new tests in `test_pin_share_chain.py`, covering both
   detection origins, the label fallback, the "explicit share still shows the pin" success
   case, and the mixed-group case. **Confirmed: 50/50 passed** across `test_pin_share_chain.py`
   and `test_map_pin_share_detection_integration.py` together (the 5 new tests plus all
   pre-existing tests in both files, no regressions). One of the 5 new tests initially failed on
   a test-authoring bug, not a real defect — Django's autoescaping renders the fixture pin name
   `Sender's Private Cabin` as `Sender&#x27;s Private Cabin`, and the first version of the test
   asserted the raw unescaped string; fixed the assertion, re-ran, confirmed. Did not cover the
   separate `PinShareDetailView` leak — see fix #8.
8. **`PinShareDetailView` leaked the sender's live `pin.description` (private notes).** The
   share-detail page (`pin_share/detail.html:38`, old) rendered `pin.description` — the sender's
   personal notes field, explicitly documented as "unrelated to `Location.description`
   (place-level info)" in `Pin.description`'s own docstring — for *any* share the recipient could
   reach, regardless of `status` or `origin`, including a `DETECTED` share never consented to and
   a share long since `ACCEPTED`/`REJECTED`. This is exactly what the earlier scoping in this
   doc's "Not yet fixed" section got wrong: it assumed both `pin.description` *and*
   `pin.address_basic` needed a new `PinShare.shared_*` snapshot field (a migration). Re-reading
   `Pin.address_basic`/`Location.address_basic` showed `address_basic` only proxies the
   *public*, canonical `Location` — the same class of data `share.place_label`/
   `share.shared_location` already legitimately expose — so it needed no protection. Only
   `pin.description` was the actual live-private-data leak. Fixed by simply removing that one
   line from the template; no model change, no migration, no new call sites to update. 4 new
   tests in `test_pin_share_detail_view.py`, including a nested loop over every
   `(PinShareStatus, PinShareOrigin)` combination (each combo gets its own recipient — the model
   enforces at most one pending, and separately at most one map-detected, share per
   `(pin, to_profile)` pair, so reusing one recipient across combos would trip a constraint
   instead of testing the fix), a post-share-edit-stays-hidden case, and a confirms-`address_basic`
   still-renders case. **Confirmed: 6/6 passed, 20/20 subtests passed.**
9. **Pin-to-wiki shared photos: deleting from either side destroyed the other side's copy, with
   zero protection (interim fix; the underlying "same row, not a copy" architecture is still an
   open question — see below).** `WikiCreationService._seed_photos` and `PinGalleryBulkView`'s
   "send to wiki" action both repoint an existing pin `Image` row's `wiki` FK rather than copying
   it — `pin_id` and `wiki_id` end up set on the *same* row. Unlike the pin-to-pin sharing case
   (which at least reference-counts the underlying storage *file* across separate rows via
   `delete_stored_file`), nothing checked for a second owning FK before deleting the *row itself*
   — `PinImageView.delete`, `WikiImageView.delete`, `PinGalleryBulkView`'s bulk delete, and the
   mobile external API's `PhotoDetailView.delete` (`external_api/views.py`) all called
   `image.delete()` unconditionally. Confirmed exploitable from all four surfaces, including the
   mobile API (`_OwnedImageMixin` scopes only by `profile__user`, not by pin/wiki, so a dual-owned
   row is reachable there too). Fixed with two new helpers,
   `detach_image_from_pin`/`detach_image_from_wiki` (`services/media/images.py`) — each nulls out
   its own FK and saves (instead of deleting) when the *other* FK is still set, otherwise falls
   back to the exact previous behavior (`delete_stored_file` + `image.delete()`). This mirrors
   the FK's own `on_delete=SET_NULL` behavior for whole-pin/whole-wiki deletion, applied to a
   single explicit photo delete too — not a new invented behavior. `PinGalleryBulkView`'s batch
   delete splits its batch into a destroy group and an unlink group instead of one blanket
   `images.delete()`. New file `test_pin_wiki_image_dual_ownership.py` (12 tests: unit-level for
   both helpers, `PinImageView`/`WikiImageView` endpoint-level, and mixed-batch/all-dual/all-solo
   cases for the bulk endpoint) plus 2 new tests in `test_external_api_photos.py` for the mobile
   surface, plus the existing `test_shared_image_file_deletion.py` (pin-to-pin case) re-run as a
   regression guard.
   **Second, pre-existing bug found and fixed while writing the batch tests, unrelated to dual
   ownership:** `PinGalleryBulkView`'s bulk-delete response reported `"deleted"` as the count of
   rows that *had a stored file* (`len(image_paths)`, filtered `if image.image`), not the count of
   rows actually deleted — a row with no stored file (e.g. one still processing) was deleted
   correctly but silently excluded from the count the client-side toast reads. This predates this
   round's changes (the original code had the identical `len(image_paths)` pattern); the batch
   tests' `baker.make(Image, ...)` fixtures (no real file attached, unlike the dual-ownership
   tests' `_make_stored_image` helper) happened to be the first thing to exercise it. Fixed by
   reporting `len(to_destroy)` (the actual row count) instead.
   **Confirmed: `test_pin_wiki_image_dual_ownership.py` 12/12 passed,
   `test_external_api_photos.py`'s new `PhotoDeleteDualOwnershipTests` 2/2 passed,
   `test_shared_image_file_deletion.py` 5/5 passed (no regression to the pin-to-pin case).** The
   fix's design was independently checked against the model's `on_delete=SET_NULL`
   FKs before being written, not just guessed. Does **not** cover `WikiImageView.post`'s
   reposition endpoint, which still mutates the shared row's live lat/lng from either side — see
   open questions.
10. **`memories/locations.html` still had the bespoke select-toggle button `visits.html` was
    already fixed to not have.** Registered a new `select_pin_suggestions` `MapToolSpec`
    (`templatetags/map_components.py`, mirroring `select_unlogged_visits`'s exact pattern — same
    icon/label, page-specific `button_id` matching what `pin-select-map.js`'s existing
    `selectToggleBtnId: 'pin-suggestions-select-toggle'` config already expected, so no JS change
    was needed) and swapped the template to `{% map_toolbar "select_pin_suggestions" ... %}`. New
    `test_map_uses_the_shared_toolbar_not_the_bespoke_pill_button` in `test_pin_suggestions.py`,
    mirroring the existing `visits.html` regression guard exactly. Template verified to load via
    Django's loader and the tool verified registered correctly. **Confirmed: this new test passed**
    as part of the 100/101 `test_pin_suggestions.py` batch run — the one failure in that file was
    an unrelated pre-existing test-fixture staleness bug the same run caught; see fix #13 below.
11. **Cross-pin aggregate comparison used raw `Location` rows instead of `Place` in four places.**
    See [Cross-pin aggregate comparison level](#cross-pin-aggregate-comparison-level) above for
    the full defect description. `services.pins.common_pins._pinned_keys` made public as
    `pinned_place_keys`; `Profile._have_common_pin` now calls it directly;
    `Profile.visible_profile_pks`/`viewers_who_can_see`
    (`models/profile/model.py`) and `trip_visibility.apply_trip_visibility_filter`'s `COMMON_PIN`
    branch now compute the same place-with-location-fallback key set in batch-safe form (a direct
    per-pair call would reintroduce the N+1 queries these batch paths exist to avoid).
    `wiki_community_summary` (`community_counts.py`) now counts root pins across every `Location`
    sharing `wiki.place_id` instead of only the single `Location` argument passed in, falling back
    to that Location when it has no Place. New tests: `test_common_pin_across_different_
    locations_sharing_a_place` in both `VisibleProfilePksAgreementTests` and
    `ViewersWhoCanSeeAgreementTests` (`test_identity_visibility_batch.py`, using the file's own
    `assert_agrees` helper so the batch and single-subject paths are checked against each other,
    not just against a hardcoded expectation); two new tests in `test_trip_visibility_is_
    stricter.py` (one proving the fix, one a regression guard that a *genuinely* unrelated Place
    is still correctly hidden — this file exists specifically to make any widening of trip
    visibility deliberate, and the new tests document that this fix narrows a gap without
    loosening that stricter contract); three new tests in `test_community_counts.py` using
    `mock.patch(..., wraps=...)` to inspect the exact `exact_count` `wiki_community_summary`
    computed, sidestepping the fuzz/threshold logic. **Confirmed: `test_identity_visibility_
    batch.py` 26/26 passed, `test_trip_visibility_is_stricter.py` 9/9 passed, `test_community_
    counts.py` 8/8 passed, `test_common_pins.py` (regression guard for the existing single-Location
    case) 21/21 passed.**
12. **Two structural pre-commit checks had no CI backstop, and both had a latent Windows bug.**
    `check_outage_not_cached.py`/`check_notification_choke_point.py` (`.pre-commit-config.yaml`)
    were never added to `.github/workflows/ci.yml`, unlike their three siblings
    (`check_imports_tracked.py`/`check_migration_graph.py`/`check_doc_line_refs.py`, added in the
    2026-08-11 audit) — a commit made with `--no-verify`, without pre-commit installed, or through
    a hookless path (GitHub's web UI) reintroduces either defect class with no backstop. While
    verifying both scripts pass cleanly before wiring them in, found both exempt test/migration
    files via `"/tests/" in str(path)` — a **Windows path-separator bug**: `pathlib` renders
    Windows paths with backslashes, so this substring check silently never matches on Windows,
    incorrectly flagging 15 legitimate test-file exemptions as violations when run locally on this
    machine (it happened to pass on Linux/CI, where `str(path)` does use forward slashes, but was
    genuinely broken for any Windows contributor running pre-commit locally). Fixed both to use
    `path.as_posix()`, which is forward-slash-normalized on every platform. **Confirmed: both
    scripts now report zero offences via direct invocation** (the same invocation now added to
    `ci.yml`); no pytest wrapper exists for any `bin/check_*.py` script in this codebase (they're
    tested by direct invocation only, matching their own "safe to run by hand" convention), so none
    was added here either. The already-known release-branch CI trigger gap is **not** fixed this
    round — a bigger infra decision, not a mechanical script-wiring fix — see open questions.
13. **Not a GOALS.md defect — a stale test fixture the 207-test verification batch (fixes #9-#11)
    caught as its one failure.** `PhotoLocationScanPhotoUploadViewTests::test_valid_upload_
    creates_an_unattached_candidate_image` (`test_pin_suggestions.py`) got `400` instead of `201`.
    Traced before assuming it was this round's regression: `tools.py:630`'s content-type check
    reads correctly (`content_type.startswith("image/")`, confirmed via `git diff`/direct read —
    no live bug there), so the failure was downstream, in `image_upload_error`'s magic-byte
    sniffing (`photo_is_not_an_image_error`, `services/security/content_sniffing.py`) rejecting the
    test's placeholder bytes (`b"fake-jpeg-bytes"`, never a real image) as not an image at all. That
    check's own docstring dates it to 2026-08-24 — a different, unrelated concurrent session's
    security fix (see that file's `docs/PROBLEMS.md` reference) landed a genuine content-sniffing
    hardening after this test's fixture was written, and the fixture was never updated to match.
    Not a production bug and not introduced by this round's changes. Fixed by switching the test's
    default upload bytes to the project's own standard real-image fixture,
    `core.tests.images.JPEG_BYTES` (built for exactly this — see that module's docstring), instead
    of inventing a new one. **Confirmed: `PhotoLocationScanPhotoUploadViewTests` 5/5 passed**,
    re-run standalone after the fix.

## Open questions for Jess

Consolidated from the per-topic sections above — nothing below is a bug to silently fix; each is
a real product/architecture decision this audit surfaced but can't make on its own:

- **Shared photo copy-vs-reference architecture** (affects both pin-to-pin *and* pin-to-wiki
  sharing): true independent file duplication per recipient (matches "copied" literally, costs
  storage) vs. reference-counted dedup by storage key (cheaper, already shipped for pin-to-pin)
  vs. dedup-by-content-hash using the existing `checksum` field. Both sides now have an interim
  guard against outright data loss on *delete* (pin-to-pin's `delete_stored_file` reference-count,
  pin-to-wiki's new `detach_image_from_pin`/`detach_image_from_wiki` unlink-not-destroy — see fix
  #9), but neither is "copied" in the literal sense the goal states, and the pin-to-wiki side
  still has no protection at all against *mutation*: repositioning a dual-owned photo's lat/lng
  from `PinImageView.post` or `WikiImageView.post` changes the one row both surfaces read, live,
  from either side. Whatever's decided, add an explicit `Image.copied_from` provenance FK rather
  than reconstructing lineage from a filename-string match (pin-to-pin) or the raw fact of a
  shared row (pin-to-wiki), and either resolve the reposition-mutation gap the same way delete was
  just resolved, or confirm live-reposition is acceptable for now.
  See [Pin-to-pin sharing](#pin-to-pin-sharing) and [Pin-to-wiki sharing](#pin-to-wiki-sharing).
- **DM/group-chat plaintext**: does "no way to turn it off" mean the client defaults to
  encrypting with no UI toggle (already true), or the server refuses to ever persist a plaintext
  message (not true today, and would need to handle the locked-device/unenrolled-partner cases
  the client currently falls back for)? See [DM/group-chat encryption](#dm-group-chat-encryption).
- **Backup encryption architecture**: needs a key-management design (where does the decryption
  key live?) before it's implementable — currently zero code toward it.
- **Backup/self-destruct interaction**: accept a bounded exposure window for messages that were
  live at backup time (documented, not enforced), or build active post-backup scrubbing?
- **List manual-remove persistence**: was "manual decisions persist" meant to cover removal too
  (GOALS.md's wording suggests yes; only addition is implemented), or is only addition in scope?
  If removal is in scope, it needs a real data-model change (an exclusion state), not just a UI
  affordance.
- **Public-pin visibility**: should a PASSED location's live wiki page actually become viewable
  by any logged-in profile, or is today's behavior (surfaces only via the opt-out suggestion
  queue and demo export; the wiki page itself stays gated) the intended design? There's dead code
  in `public_vote_context` consistent with the more permissive reading once having been intended.
- **Trivia/Facts integration**: is Trivia intentionally exempt from the general `Fact`/
  `FactEvidence` schema (its own simpler vote-threshold-gated pipeline), or should it route
  through `services.facts.evidence` like SpotGuessr/Consensus do? Relatedly, the `built_year`
  Fact registry key has zero producers anywhere — dead entry or missing wiring?
- **Concealed users**: ~60 commits building this already exist on `origin/@release/v_0_7_0`
  (diverged 34/199 commits from current `HEAD`; `main` is 958 commits behind that release
  entirely). Needs a deliberate merge/rebase/cherry-pick decision — and that branch's own history
  records its own adversarial-review failures, so a port should re-review critically rather than
  merge wholesale.
- **`global_search.WikiSearchProvider`'s search-relevance scope**: now fixed to match the page's
  domain-access rule (see fix #4 above) — worth Jess confirming that's the wanted behavior rather
  than a deliberately narrower "search relevance" scope, since it was ambiguous which was
  intended.
- **Daphne/nginx access-log rotation**: the docker-compose `logging:` fix (#3 above) bounds each
  service's stdout/stderr, but daphne's `--access-log` (persistent volume, unbounded) and nginx's
  `/tmp/nginx-requests.log` (container-ephemeral but still unbounded until restart) both need a
  real rotation mechanism — a logrotate sidecar, or reconsidering the logging strategy — which is
  an infrastructure decision, not a compose one-liner.
- **`shared_name`'s live-name behavior**: `PinShare.shared_name`'s docstring documents that a
  blank value deliberately tracks the sender's pin's *current* name at both share and accept
  time — the one sanctioned live-reference exception. Worth confirming that's still wanted now
  that the privacy goal is stated this explicitly. (Fix #8 resolved the pin-share detail page's
  actual leak — `pin.description`, the sender's private notes — by simply not rendering that
  field; it turned out no new `shared_*` snapshot fields were needed at all, since the other
  field the original scoping worried about, `pin.address_basic`, only proxies the already-public
  `Location`. So `shared_name` remains the *only* live-reference field on `PinShare`, not one of
  several — which sharpens this question rather than resolving it.)
- **Single-user/self-hosted deployment scope**: does "not an afterthought" mean a dedicated
  lightweight compose profile (drop valkey/celery-worker-panels/celery-beat/clamav/app-ws), or
  does the already-true "no Kubernetes required" satisfy the goal with "desktop-style" read as
  softer framing than the current 9-service stack implies? See
  [Self-hosting and federation](#self-hosting-and-federation).
- **Release-branch CI trigger gap**: `ci.yml`/`security.yml` still trigger only on
  `pull_request: branches: [main]` (known since 2026-08-16 per project memory, re-confirmed live
  in code this round) — worked around via manual `gh workflow run --ref`, not fixed. Widening the
  trigger is a real infra decision (cost/behavior implications on every future release PR), not a
  mechanical fix like the two check-script CI additions in fix #12.
- **i18n scope**: is translation infrastructure meant to be built now (beta, ~2 users per project
  memory) or does it genuinely block the stated Europe relocation? If near-term, it needs real
  scoping — `LocaleMiddleware` + `LANGUAGES` + `LOCALE_PATHS`, wrapping the many hardcoded
  template/JS strings, and at least one real compiled `.po`/`.mo` proving the pipeline works
  end-to-end. See [i18n and observability](#i18n-and-observability).
- **Uptime-monitor alerting location**: does this belong in the app repo at all, or does it
  already live in `../infrastructure`/a third-party dashboard (per project memory), in which case
  the goal may already be satisfied and `docs/GOALS.md`/this audit should say so rather than
  reading as an in-app gap? See [i18n and observability](#i18n-and-observability).
- **Boundary/region-drawing maps and the floorplan editor's bespoke toolbar**: intentional,
  permanent exceptions to the shared map-controls system worth documenting (like the safety
  check-in's readonly exception), or technical debt worth migrating? Same question for Consensus/
  SpotGuessr's non-standard game-map toolbar treatment. See
  [Map UI consistency](#map-ui-consistency).
- **Trip photo archive**: should this be a first-class linkage (`Image.trip_activity` FK or a
  trip-level gallery) at all, or was the goal always "photos on pins that happen to be part of a
  trip"? This is a scoping question that blocks the encryption question entirely, since there is
  currently no trip-photo concept to encrypt. See
  [Trip photo archive encryption](#trip-photo-archive-encryption).
- **Cross-pin-aggregate shared-helper consolidation**: fix #11 leaves `pinned_place_keys` reused
  directly at one call site and the same logic duplicated in batch-safe form at two others (plus
  `wiki_community_summary`'s own single-Location-vs-place variant) — worth extracting one
  canonical helper all four/five sites route through, given how many independent copies of this
  bug already existed, but that's an API-surface decision. See
  [Cross-pin aggregate comparison level](#cross-pin-aggregate-comparison-level).

## Not yet fixed, no open design question — just not reached yet

- **`WikiImageView.post`/`PinImageView.post` reposition endpoints still mutate a dual-owned
  photo's row live from either surface.** Fix #9 stopped *delete* from destroying a dual-owned
  pin+wiki photo's row, but reposition (dragging the photo's map marker) still writes
  `latitude`/`longitude` directly onto the one shared row with no dual-ownership check at all —
  so moving a shared photo's pin on the pin's own gallery map silently moves it on the wiki's
  gallery map too, and vice versa. Same underlying architecture ("one row serves two owners") as
  fix #9, different manifestation (mutation, not destruction) — deliberately left alone this
  round since "should a reposition from one side even be allowed to affect the other's view, or
  should the marker position be per-surface" is closer to a real design question than delete's
  "never destroy data you don't own" was.
