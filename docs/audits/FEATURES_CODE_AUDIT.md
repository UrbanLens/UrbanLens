# FEATURES.md vs. code: audit

Generated 2026-08-25, by reading the actual implementation for concrete claims in
`docs/FEATURES.md` (a codebase-generated feature inventory, last verified/expanded 2026-07-29) -
not by reading other docs. This is a different kind of check than `docs/GOALS_CODE_AUDIT.md`:
`FEATURES.md` describes what the code currently does rather than what it's obligated to do, so
most drift here is the doc falling behind fast-moving feature work, not the code violating an
intent. Several findings were live security/privacy gaps regardless.

Twenty-five topics across three rounds were chosen for concreteness (a falsifiable claim with a
specific mechanism named). Round 2 deliberately re-tested the exact bug shape round 1 found once
(a web-UI view correctly applies a gate via a shared service function; a parallel external_api
endpoint queries the same data directly and forgets to) - and found it twice more. Round 3 covered
the sections named "not yet audited" after round 2: Mapping & Pins, External Data Enrichment,
Labels, Custom Fields, External Photo Integrations, Account & Auth, Trivia, and Consensus.
`FEATURES.md` is 893 lines; twenty-five topics is still a partial pass - see "Not yet audited"
below.

**How to read status:** `MATCHES` = the code does exactly what the doc says, today. `PARTIAL` =
mostly true with a real caveat. `STALE` = the doc was accurate once but the code has since
changed (a documentation-currency issue, not a live bug). `CONTRADICTS` = the code does something
different from - or the opposite of - what the doc claims, today.

Every `STALE`/`CONTRADICTS` finding was independently adversarially verified (a second agent
tried to refute it by re-reading the cited code itself); all survived - rounds 1-2's three plus
round 3's three (`"organize this property?"` mirroring, Labels, and Consensus). `PARTIAL` findings
were not auto-verified - each was checked by hand (against code, not just the reporting agent's
word) before acting on it.

## Summary

| Topic | Status | Headline |
|---|---|---|
| [Notification mute enforcement](#notification-mute-enforcement) | MATCHES | Per-person, single choke point, CI-backed - no gaps found |
| [Device-scan retrieval privacy](#device-scan-retrieval-privacy) | MATCHES | Only a cumulative, unattributed marker is ever readable; purpose-built regression suite already guards it |
| [Property-owner subscriber gate](#property-owner-subscriber-gate) | ~~CONTRADICTS~~ **fixed** | External/mobile API returned official owners' name/address/phone/email to *any* API key, unlike the web UI - fixed |
| [Achievements permanence](#achievements-permanence) | MATCHES | Awards are genuinely insert-only; no `post_delete` handler exists to revoke one |
| [SpotGuessr eligibility](#spotguessr-eligibility) | ~~STALE~~ **doc updated** | "No caching across sessions" stopped being true the day after this doc text was written (prewarm cache added) - not a live bug, doc corrected |
| [External API key scope](#external-api-key-scope) | ~~STALE~~ **doc updated** | Doc still described the feature's original "read uuid, create pins" shape; the surface has grown to 100+ endpoints / ~30 scopes - doc corrected |
| [Group-chat feature scope](#group-chat-feature-scope) | ~~STALE~~ **doc updated** | Reactions now work for group messages via the API/mobile surface (backend + WS), just not the web UI yet - doc corrected |
| [Undo restore safety](#undo-restore-safety) | ~~PARTIAL~~ **fixed** | 7 of 8 undo handlers pre-check FK constraints and refuse cleanly; `TripUndoHandler` didn't, and could 500 if a creator/member's profile was deleted during the retention window - fixed |

### Round 2 - deliberately re-testing the external-API-forgets-a-gate bug shape

| Topic | Status | Headline |
|---|---|---|
| [Nearby-research feature gate](#nearby-research-feature-gate) | ~~CONTRADICTS~~ **comment corrected** | The one gated panel (EPA) is symmetric and correct on both surfaces - no bypass. But `SiteFeature.NEARBY_RESEARCH`'s own code comment implied 7 newer sibling panels share the gate; they don't, by design - comment corrected |
| [Web-search feature gate](#web-search-feature-gate) | MATCHES (audit premise was wrong) | Web Images was never claimed to be paid by `FEATURES.md` or gated by `SiteFeature.SEARCH` in code - no doc or code issue |
| [Cost-transparency page gate](#cost-transparency-page-gate) | MATCHES | `/costs/` 404s correctly until enabled; no external_api cost/billing surface exists at all |
| [Login lockout coverage](#login-lockout-coverage) | PARTIAL, open question | Web login has full lockout+throttle; external API key auth and the OAuth2 token endpoint have none - flagged for Jess, not fixed (see below for why) |
| [Public-locations anonymous voting](#public-locations-anonymous-voting) | MATCHES | No running tally ever leaves the server before a vote settles, on any surface |
| [Floorplan personal-by-default scoping](#floorplan-personal-by-default-scoping) | ~~PARTIAL~~ **fixed** | Access scoping is correct everywhere; the GeoJSON features endpoint under-served published plans (dead code today, but real) - fixed |
| [Auto-source deletion permanence](#auto-source-deletion-permanence) | ~~CONTRADICTS~~ **fixed** | External API's wiki alias/link DELETE endpoints skipped the tombstone the web UI records, so a mobile deletion could be silently undone by the next auto-sync - fixed |
| [PWYW dynamic-threshold re-evaluation](#pwyw-dynamic-threshold-re-evaluation) | ~~PARTIAL~~ **doc updated** | Core mechanic matches; doc omitted the banked-overpayment access path and undersold the recompute cadence (nightly sweep, not just per-charge) - doc corrected |

### Round 3 - the sections "Not yet audited" named: Mapping & Pins, External Data Enrichment, Labels, Custom Fields, External Photo Integrations, Account & Auth, Trivia, Consensus

| Topic | Status | Headline |
|---|---|---|
| [Pin/Place scope model and tombstones](#pin-place-scope-model-and-tombstones) | ~~PARTIAL~~ **fixed** | Pin/Location/Place model all match; the alias/link/label/owner "deletion is permanent" tombstone promise had two real gaps - bulk pin-edit and the map's quick-edit dialog could remove a label without recording one - fixed |
| ["Organize this property?" and pin↔wiki sync](#organize-this-property-and-pinwiki-sync) | ~~STALE~~ **doc updated** | Building mirroring no longer gates on a pre-existing community wiki (it seeds a draft one); everything else in this claim cluster matches - doc corrected |
| [Property-records web-UI gate](#property-records-web-ui-gate) | MATCHES | The web UI's Ownership/Sale History cards filter through the same `visible_owners`/`sale_rows` gate as the (already-fixed) external API - no drift between the two surfaces |
| [External-data caching and cost tracking](#external-data-caching-and-cost-tracking) | ~~PARTIAL~~ **doc updated** | Caching/rate-limiting/background-enrichment all match; the doc overstated the public `/costs/` page as a per-service 30-day breakdown - it's a coarser blended figure - doc corrected |
| [Labels: kind count and shared-feature claims](#labels-kind-count-and-shared-feature-claims) | ~~CONTRADICTS~~ **doc updated** | A fifth kind (Media labels) was missing entirely from the doc; bulk-convert and drag-reorder are deliberately restricted to 3 of the 5 kinds, not shared across all of them - doc rewritten |
| [Custom Fields and External Photo Integrations](#custom-fields-and-external-photo-integrations) | MATCHES | All four Custom Field targets real and private; all four photo integrations match, including a verified-unbypassable 100-photo cap on public Flickr album import |
| [Account & Auth claims](#account-auth-claims) | MATCHES | Login lockout (per-identifier + per-IP), OAuth password→E2EE unlock, deletion grace period, and the default API-key scope grant (exactly `PROFILE_READ`/`PINS_READ`/`PINS_WRITE`/`PUSH_MANAGE`, hashed, immediately-revocable) all check out |
| [Trivia content-safety claims](#trivia-content-safety-claims) | MATCHES | Classifier fails closed on every AI-unavailability path; multiplayer structurally cannot surface an unapproved question; the four moderation/generation/answer-check/wiki-incorporation toggles are genuinely independent |
| [Consensus trust model](#consensus-trust-model) | ~~CONTRADICTS~~ **doc updated, open question raised** | Trust posterior is real but never gates the actual wiki write - a brand-new account has equal write power to a veteran; the promised cross-session tentative-answer promotion path is dead code - doc corrected to describe reality, flagged below as the round's highest-priority open question |

---

## Notification mute enforcement

**Claim** (`docs/FEATURES.md` "Social Layer"): mute is per-person and actually silences
everything hanging off a notification, applied in one place (`NotificationLog.objects.notify`)
and enforced by `bin/check_notification_choke_point.py`.

**Verdict: MATCHES.** Mute is two boolean columns on the shared `Friendship` row
(`muted_by_from_profile`/`muted_by_to_profile`, `models/friendship/model.py:59-60`), read
directionally through `Friendship._mute_field_for` so muting someone never mutes you to them.
`NotificationManager.notify()` (`models/notifications/queryset.py:53-112`) is the only place that
turns the preference into silence, skipping the row entirely for a muted recipient outside
`MUTE_EXEMPT_TYPES` (the 8 safety-check-in/partner types). Every production call site uses
`.notify()` - a repo-wide search for a bypassing `NotificationLog.objects.create`/`bulk_create`/
`get_or_create`/`NotificationLog(...)` call turned up matches only in test files, which the
checker deliberately exempts. All three downstream channels (WS toast, WhatsApp/SMS, native push)
hang off `NotificationLog`'s `post_save` signal, so skipping the write silences all of them.
`bin/check_notification_choke_point.py` is wired into both `.pre-commit-config.yaml` and
`.github/workflows/ci.yml` (the latter added this same audit round, in `GOALS_CODE_AUDIT.md` fix
#12), and no production call site currently uses its `notify-bypass-ok:` escape hatch.

No action needed.

## Device-scan retrieval privacy

**Claim** (`docs/FEATURES.md` "Device Scanning"): individual scans are never retrievable through
any API - only the cumulative, unattributed marker per (device, wiki) is ever readable, and only
for wikis the caller has already discovered.

**Verdict: MATCHES.** Exactly two external-API routes exist for this feature: a write-only upload
that returns only an `upload_uuid` (no scan data echoed back), and a read-only `nearby/` endpoint
returning `WikiDeviceMarker` rows - cumulative fields only (confidence, an averaged signal
strength, centroid, status), no per-scan or per-uploader data, filtered to wikis the caller has
already discovered via the same domain-based `visible_wiki_location_ids` gate used elsewhere in
the app. `WikiDeviceMarker` itself has no profile/uploader FK at all. No internal `/rest/`
viewset, admin registration, or template exposes any device-scan model individually. This
invariant is guarded end-to-end by a dedicated regression suite
(`test_device_scan_privacy.py`) already in the tree - model-shape, route-count, serializer
allowlist, and a two-uploader end-to-end test proving the marker collapses to one anonymous row.

No action needed. (One minor non-contradicting nuance the auditing agent flagged: scan-upload
ingestion resolves a marker UUID with no wiki-visibility check of its own - but nothing about that
upload path is ever read back, so it doesn't affect this claim.)

## Property-owner subscriber gate

**Claim** (`docs/FEATURES.md` "External Data Enrichment"): owner names and contact details from
`OFFICIAL`-sourced records are subscriber-only (`SiteFeature.PROPERTY_OWNERS`, enforced in
`services.property.owner_access`) - parcel/tax/assessment/district facts stay open to everyone.

**Verdict: CONTRADICTS, fixed.** True for the web UI: `owner_access.py`'s
`can_see_official_owners()`/`visible_owners()`/`sale_rows()` gate every `OFFICIAL`-sourced
`WikiOwner`/`WikiPropertySale` row behind the feature flag, and the pin-detail panel, wiki
Ownership panel, and both Sale History tabs all route through it. But
`external_api/views_wiki.py`'s `WikiOwnershipView`/`WikiPropertySalesView` queried
`WikiOwner`/`WikiPropertySale` straight into their serializers with **no call into
`owner_access` at all**, gated only by the generic `ApiKeyScope.WIKI_READ` scope - which carries
no subscription requirement. Any user could mint an API key and pull paid official-owner
name/mailing-address/phone/email data for free through this endpoint. Independently
adversarially verified (confirmed).

**Fixed** in `external_api/views_wiki.py`:
- `WikiOwnershipView.get` now filters through `owner_access.visible_owners(..., profile.user)`
  before pagination - the same function the web UI calls, so the two answers can't drift apart.
- `WikiPropertySalesView.get` builds a `SimpleNamespace` copy of each sale with its
  `previous_owners`/`new_owners` pre-filtered per caller, rather than mutating the real
  `WikiPropertySale` instance - an earlier draft of this fix called
  `sale.previous_owners.set(...)` to shape the response, which would have **persisted the
  withheld-for-this-caller list as the sale's actual recorded parties**, destroying the official
  record for every other viewer too. Caught before landing; a regression test now asserts the
  stored M2M relation is untouched after a filtered read.

New file `test_external_api_property_owners.py`: `WikiOwnershipApiGateTests` (3 tests - plain-key
gating, subscriber-key access, contributed-only-owner parity) and `WikiPropertySalesApiGateTests`
(3 tests - plain-key gating, subscriber-key access, and the DB-mutation regression guard above).
**Confirmed: 6/6 passed**, plus the existing `test_property_owner_access.py` (the web-UI half of
this same gate) re-run as a regression guard, **15/15 passed** - no drift between the two
surfaces' answers.

## Achievements permanence

**Claim** (`docs/FEATURES.md` "Achievements"): awards are permanent - deleting pins lowers the
metric but keeps the award.

**Verdict: MATCHES.** Metrics like `pins_created` are computed live from current DB state, so
deleting a pin genuinely lowers what the metric reports. But `UserAchievement` grants are
insert-only: `evaluate.py`'s `_grant` only ever calls `get_or_create`, no code path anywhere ever
calls `.delete()` on a `UserAchievement`, and `models/achievements/signals.py` states outright
that no `post_delete` handler exists on purpose. Asserted end-to-end by
`test_achievements.py::test_award_survives_the_metric_falling_back_below_threshold`.

No action needed.

## SpotGuessr eligibility

**Claim** (`docs/FEATURES.md` "Games: SpotGuessr"): a location is only ever offered if pinned by
every *joined* participant, no exceptions, no caching across sessions.

**Verdict: STALE, doc updated.** The JOINED-vs-INVITED gating is exactly true today -
`eligibility.eligible_locations()` AND-filters per profile, every caller builds that profile list
from `session.participants.joined()`, and the roster locks before a session goes `ACTIVE`. The
"no caching across sessions" half stopped being true one day after this doc text and
`eligibility.py` were both added (commit `abb0f30d`, 2026-07-30): `services/spotguessr/prewarm.py`
(commit `691b5c31`, 2026-07-31) added a `django.core.cache`-backed layer that pre-picks a
(location, content) pair up to 20 minutes ahead and redeems it without re-running eligibility at
redemption time. Not a live access-control bug - the pick is still eligibility-checked at prewarm
time, so no participant is ever shown a location they didn't have pinned - just a doc that was
never updated when caching was added the next day. Independently adversarially verified
(confirmed).

**`docs/FEATURES.md` corrected** to note the prewarm cache exists and that it only ever redeems an
already-eligibility-checked pick.

## External API key scope

**Claim** (`docs/FEATURES.md` "Account & Auth", "REST API"): external API keys are "an extremely
limited, scoped grant - currently reading only the owner's uuid and creating pins."

**Verdict: STALE, doc updated.** This described the feature's original, minimal shape. Today
`external_api/urls.py` wires 100+ endpoints (pins full CRUD, photos, wikis, trips, messaging,
friends, notifications, safety check-ins, lists, labels, custom fields, undo, panels, assistant,
games, search, device scans, and more) behind a ~30-member `ApiKeyScope` vocabulary - driven by
the mobile-app-parity work. Even a freshly issued key's *default* grant
(`PROFILE_READ`/`PINS_READ`/`PINS_WRITE`/`PUSH_MANAGE`) already covers reading, PATCH-editing, and
DELETE-ing all of the owner's pins, not just creating one. This is a legitimate, well-built
expansion (per-scope enforcement via `HasApiKeyScope`, OAuth2-only gating for the scopes that need
user consent, deliberate non-widening of already-issued keys when new scopes were added) - not a
live access-control bug - but the doc's specific factual claim is false even for a brand-new key.
Two in-code docstrings (`external_api/__init__.py`, the `ApiKey` model) are equally stale, so this
appears to be a case of nobody having refreshed any of this documentation since the scope
vocabulary grew past its original two values. Independently adversarially verified (confirmed).

**`docs/FEATURES.md` corrected** to describe the current scope model rather than the original one.
The two stale in-code docstrings are lower-stakes (they're read by developers with the actual
`ApiKeyScope` enum one file away, not end users) and were left for a separate pass - see open
items below.

## Group-chat feature scope

**Claim** (`docs/FEATURES.md` "Direct Messaging"): group-chat scope is deliberately behind 1:1
DMs "as of 2026-07-18" on nine named features, including reactions.

**Verdict: STALE, doc updated.** Every named gap is still accurate except reactions. Group
message reactions are now fully implemented - `Reaction.group_message` FK with its own unique
constraint, a complete `toggle_group_reaction` service with live WebSocket broadcast, and a mobile
API view (`GroupMessageReactionView`, external_api - all added 2026-07-29, after this doc text's
stated cutoff). But the primary web UI's group-chat templates have no reaction picker and no
handler for the `group_reaction` WS event, so the doc was correct for the website and is now
wrong about the platform as a whole. (`docs/FEATURES.md`'s own WebSockets section already says
"reaction updates ... for DMs and group chats" - an internal inconsistency that's additional
corroborating evidence, now resolved by this fix.) Independently adversarially verified
(confirmed).

**`docs/FEATURES.md` corrected** to move reactions out of the "still missing" list and describe
the API/mobile-vs-web-UI split accurately.

## Undo restore safety

**Claim** (`docs/FEATURES.md` "Undo / Data Safety"): restores pre-check the constraints the
recreate could violate and refuse cleanly rather than 500ing; relational pieces that were never
part of the deletion restore leniently, skipping whatever has since been deleted.

**Verdict: PARTIAL, fixed.** True for 7 of the 8 undo-able types (pin, wiki, safety check-in,
saved filter, label, pin list, markup map) and for all three lenient-restore examples the doc
names by example. `TripUndoHandler.restore()` was the one handler never revisited after this
pre-check pattern was retrofitted onto the others: it recreated a `Trip` from
`entry["creator_id"]` and each `TripMembership` from a stashed `profile_id` with **no existence
check at all**. `Trip.creator` is `SET_NULL`-nullable but that doesn't protect a *stale non-null*
id from violating the FK constraint at the DB level, and `TripMembership.profile` isn't nullable
at all - so a trip whose creator, or any roster member, independently deleted their account
during the 7-day retention window raised an uncaught `IntegrityError` on restore instead of the
documented clean refusal. Confirmed by git history that this is a real gap in the doc's universal
wording (introduced 2026-08-08 by generalizing from six recently-hardened handlers) rather than
a claim that used to be true and later regressed - `trip.py`'s substantive logic hadn't changed
since 2026-07-09.

**Fixed** in `services/undo/handlers/trip.py`: `restore()` now pre-checks that the creator
profile exists and that every roster member's profile exists, raising `UndoExpiredError` (the
same exception every other handler uses for this) before any `Trip.objects.create()` call -
mirroring `PinUndoHandler`'s own treatment of its profile/wiki FKs, which also fail closed rather
than silently null out a stale reference. New tests in the existing
`test_undo_restore_conflicts.py::TripUndoConflictTests`: a deleted-creator case, a deleted-member
case (both asserting a clean `UndoExpiredError`, not a crash), and a regression guard that an
intact roster still restores completely (rsvp, is_organizer, and all). **Confirmed:
`test_undo_restore_conflicts.py` 10/10 passed** (7 pre-existing + 3 new), plus the broader
`test_undo.py` suite re-run as a regression guard, **30/30 passed**.

A lenient alternative - silently dropping just the missing member from the restored roster
instead of refusing the whole trip - was considered and deliberately not built: unlike a list's
member pins or a label's parents (relational pieces genuinely incidental to the thing being
restored), a trip's roster is user-facing enough that silently shrinking it without any signal
seems like its own surprise. Flagged below as an open product question rather than guessed at.

## Nearby-research feature gate

**Claim** (`docs/FEATURES.md` "External Data Enrichment", implied by `SiteFeature.NEARBY_RESEARCH`'s
own comment): nearby-facility/feature research tabs on the Private Pin page - EPA's
nearby-regulated-facilities list, Cameras & Structures, Underground Structures, Permits &
Violations, Reported Incidents, Water & Hydrology, Site Conditions, Fire & Disaster History - are
gated behind a paid `SiteFeature`, separate from each plugin's own free "data about this exact
pin" card.

**Verdict: CONTRADICTS the implied generalization, not `docs/FEATURES.md`'s actual prose - fixed
via a code comment, no behavior change.** Exactly one panel declares `required_feature`:
`EpaEchoNearbyPanelSource`, and for it the gate is enforced correctly and *symmetrically* on both
the web Private Pin page and the external API via the single shared `panel_visible_to()` function -
no bypass exists for this one. The other seven named panels never set `required_feature` (default
`None`), so `panel_visible_to()` returns `True` unconditionally for all of them, on both surfaces
- they are fully free today, by design (`test_panel_feature_gate.py`'s own docstring: "Today
exactly one source declares it"). `docs/FEATURES.md`'s own prose already reflects this accurately
(no subscriber callout on those seven, unlike Property Records' explicit one) - what overclaimed
was `SiteFeature.NEARBY_RESEARCH`'s in-code comment, written when EPA was the only nearby-data
panel and never revisited as six of the seven free siblings were added 2026-08-15/08-20.
Independently adversarially verified (confirmed).

**Fixed**: the misleading comment on `SiteFeature.NEARBY_RESEARCH`
(`models/subscriptions/model.py`) now states plainly that only EPA declares it and names the free
siblings, so a future reader checks `PanelSource.required_feature` per-panel instead of assuming
from the enum name. No test needed - a comment fix, and the underlying behavior (which the
existing `test_panel_api_interface.py::test_required_feature_is_only_set_where_the_web_gates_too`
already covers) was already correct.

## Web-search feature gate

**Claim under audit**: the Web Images gallery panel (`plugins.builtin.searxng_images`) is a
`SiteFeature.SEARCH`-gated paid feature.

**Verdict: MATCHES - this round's own audit premise was wrong.** `SearxngImageMediaSource.gate()`
checks only `redata_configured()` and a buildable query - no subscription check, no
`required_feature`. `SiteFeature.SEARCH` gates a completely different, unrelated feature (the
text-based web-search-results tab, `controllers/pin.py`'s `_web_search_response`). Critically,
`docs/FEATURES.md` itself never claims Web Images is paid - its "Web Images" bullet carries no
paid/VIP annotation, unlike Property Records' explicit "subscriber-only" callout. So there is no
doc-vs-code mismatch to fix; this topic was scoped from an incorrect inference (that
`SiteFeature.SEARCH`'s name implied it covered image search too) rather than a real claim in the
doc. No action needed.

## Cost-transparency page gate

**Claim** (`docs/FEATURES.md` "Cost Tracking"): the public `/costs/` page 404s until an admin
enables `SiteSettings.public_costs_page_enabled` (off by default).

**Verdict: MATCHES.** `CostsView.get_context_data` raises `Http404` when the flag is off; the
field defaults to `False`; the only toggle is the site-admin Costs page. A full search of
`dashboard/external_api/` found zero references to cost/billing data of any kind - no parallel
endpoint exists to bypass the gate, so the bug shape this round was hunting for structurally
cannot occur here. No action needed.

## Login lockout coverage

**Claim** (`docs/FEATURES.md` "Account & Auth"): "Login lockout after repeated failed attempts."

**Verdict: PARTIAL, deliberately left as an open question rather than fixed.** The web
session-login form is thoroughly covered: `CustomLoginView` locks an identifier out after
`SiteSettings.login_max_attempts` failures, separately throttles per-IP, and applies a matching
lockout to TOTP/backup-code entry. None of this extends to the external API's own two
credential-validation surfaces - `ApiKeyAuthentication`'s bearer-key check and the stock
`django-oauth-toolkit` OAuth2/PKCE token endpoint - which have no failure counter or lockout at
all; DRF's authentication-before-throttling ordering means even the generic throttle classes never
see a rejected credential.

**Why this wasn't fixed like the other findings**: API key secrets are generated with
`secrets.token_urlsafe(_SECRET_ENTROPY_BYTES)` (`services/auth/api_keys.py`) - cryptographically
high entropy, the same class of defense every major API provider relies on instead of lockout
(brute-forcing a specific key is computationally infeasible regardless of rate limiting, unlike a
short human password). Bolting a custom lockout onto a mature, spec-compliant OAuth2 library
carries real risk of breaking legitimate retries or clients for a benefit that's unclear given the
underlying tokens' entropy. This is a genuine gap worth a deliberate decision, not a mechanical
fix to make unilaterally - see open questions.

## Public-locations anonymous voting

**Claim** (`docs/FEATURES.md` "Public Locations"): voting is anonymous in the UI - a voter sees
only their own choice, never a running tally, before an outcome settles.

**Verdict: MATCHES.** `public_vote_context` is the only function that ever returns vote state to a
client, and it returns at most `{"is_public": bool}` or `{"is_public": False, "my_vote":
bool|None}` - never counts. The real tally (`PublicPinVoteQuerySet.tally`) is used only
server-side, in the Celery-beat eligibility engine, never on a request path. No external_api
surface for public-pin voting exists at all. No action needed.

## Floorplan personal-by-default scoping

**Claim** (`docs/FEATURES.md` "Building Floorplans"): a local plan is scoped to its author;
"Publish to wiki" extends visibility (and edit rights) to anyone who can see the place's wiki.

**Verdict: PARTIAL, fixed.** The core claim holds everywhere floorplan data is actually served in
the primary UI: every route requires pin ownership, personal-plan queries are hard-filtered to the
requester's own profile, and both the read (`resolve_document`/`_community_plan`) and write
(`publish_to_wiki`/`can_edit_community`) sides of publishing gate on `place_visible_to` - the same
function backing the wiki's own visibility rule, so the two can't drift. The one real gap: the
GeoJSON `/floorplan/features/` endpoint - the specific surface this audit asked about - never
called into that resolution logic at all, only ever resolving the caller's own personal plan by a
hard-coded `profile=` filter. A published plan was therefore invisible through this one endpoint,
even to a second user who would see it fine via the JSON document endpoint. Confirmed this is
currently dead code for the primary UI (`floorplan-editor.ts` never fetches this endpoint), so the
gap under-serves rather than leaks - the endpoint's own docstring ("for any other software that
speaks GeoJSON") overpromised relative to what it delivered. No external_api/mobile surface exists
for floorplans at all, so the specific "web-vs-parallel-endpoint" bug shape this round targeted
doesn't apply here; this is a same-surface under-service gap instead.

**Fixed**: added `resolve_floorplan_row()` (`services/floorplans/resolution.py`), the row-returning
counterpart to `resolve_document()` (same local-then-community fallback, for a caller that needs
the actual `Floorplan` row rather than a serialized dict). `FloorplanFeaturesView.get`
(`controllers/floorplans.py`) now uses it for the "current" (no `?version=`) lookup, and the
`?version=<uuid>` lookup was extended the same way (try the caller's own row by uuid, then a
published row by uuid gated on `place_visible_to`). New tests: `ResolveFloorplanRowTests` (5
tests, `test_floorplans.py`) for the new service function. **Confirmed: `test_floorplans.py`
110/110 passed** (full file, including every pre-existing class, re-run as a regression guard).

## Auto-source deletion permanence

**Claim** (`docs/FEATURES.md` "Mapping & Pins"): "Deleting an auto-added alias, link, label, or
property owner is permanent - automatic sources... won't silently recreate something you
removed."

**Verdict: CONTRADICTS, fixed.** The tombstone system (`PinAutoRemoval`/`WikiAutoRemoval`)
genuinely backs this claim everywhere the web UI touches it, and everywhere the external (mobile)
API reuses the same shared service functions (pin-side aliases/links, and the one bulk label-remove
endpoint, all correctly record a tombstone). But the external API's own wiki-alias and wiki-link
`DELETE` endpoints (`WikiAliasDetailView`, `WikiLinkDetailView` in `external_api/views_wiki.py`)
called `.delete()` directly with **no tombstone recorded at all**, unlike their web-UI
counterparts (`controllers/aliases.py`, `controllers/links.py`). A mobile-app user deleting a
community wiki alias or link would see it silently recreated the next time the external
name-provider sync or a link-adding plugin (Nominatim, EPA, Wikipedia) next ran - exactly the
outcome the doc says can't happen. (Separately, and only a doc-currency nit: auto-added
`OFFICIAL`-sourced `WikiOwner` records can't be deleted via *any* surface at all, so "permanent
deletion" is moot rather than literally false for that one sub-case.) Independently adversarially
verified (confirmed).

**Fixed**: both views now call `WikiAutoRemoval.objects.record(...)` before deleting, mirroring
the web UI's exact pattern. New tests: `ExternalApiWikiTombstoneTests` (4 tests, added to the
existing `test_auto_removals.py`) - tombstone-recorded assertions for both alias and link, plus
end-to-end regression guards proving a deleted item is not recreated by the backfill sync / EPA
auto-add-link path. **Confirmed: `test_auto_removals.py` 27/27 passed** (full file, including
every pre-existing tombstone class, re-run as a regression guard). One of the four new tests
initially failed on a self-authored fixture bug (a duplicate root-pin violating
`db_pin_unique_location_per_profile`, not a production defect) - fixed and reconfirmed.

## PWYW dynamic-threshold re-evaluation

**Claim** (`docs/FEATURES.md` "Paid Subscriptions"): dynamic-threshold PWYW features are "only
granted in billing cycles where the pledge meets or exceeds" the current cost-per-user,
"recomputed at each successful charge, on the user's own billing anniversary."

**Verdict: PARTIAL, doc updated.** The core mechanic is real: `threshold_met` is a stored boolean
on `RoleSubscription`, recomputed against the *current* `cost_per_user()` on a successful charge,
and Stripe's own `status` field is written independently - so a subscription can stay `active`
while `threshold_met` alone flips to `False`, matching the doc's claim about status not changing.
But the summary was incomplete on two points: (1) it omitted an entire second access path - a
banked pay-what-you-want usage ledger (`services/billing/banking.py`) that can keep granting
access via `has_banked_access` even when the current pledge fails the current threshold, or after
the subscription is canceled outright; and (2) "recomputed... on the user's own billing
anniversary" undersold the actual cadence - a **nightly** Celery-beat sweep
(`sync_stripe_subscriptions`, all non-canceled subscriptions, not per-anniversary) also
recomputes it, plus other qualifying Stripe webhook events beyond just charges. Not a bug - both
mechanisms work as designed - just a doc that undersold what actually governs access.

**Fixed**: `docs/FEATURES.md`'s PWYW-dynamic-threshold bullet now names the banked-overpayment
access path and the nightly-sweep cadence. No code change; no new tests (nothing here contradicts
existing behavior).

## Pin/Place scope model and tombstones

**Claim** (`docs/FEATURES.md` "Mapping & Pins"): the Pin/Location split, the Place model
(PART_OF/MEMBER_OF), parcel-vs-building scope derivation, and "deleting an auto-added alias, link,
label, or property owner is permanent."

**Verdict: PARTIAL, fixed.** The model/scope claims all match exactly. The tombstone system
(`PinAutoRemoval`/`WikiAutoRemoval`) genuinely covers all four kinds through their dedicated
single-item delete surfaces (`LabelPinMembershipView`, `delete_pin_alias`/`delete_pin_link`,
`PinOwnerRemoveView`). But two other live surfaces remove the same tag/category/status labels
without recording one: the map's multi-select bulk-edit ("remove_label_ids") and the map pin's
quick-edit dialog (`label_ids` via `.set()`/`.clear()`). A label removed through either could be
silently reattached by keyword/AI auto-tagging - the exact failure the tombstone system exists to
prevent, just reached through a side door.

**Fixed**: both call sites now record a `PinAutoRemoval` before removing, matching the dedicated
panel's pattern (`controllers/pin_bulk.py`, `controllers/maps.py`); 6 new tests in
`test_auto_removals.py`.

## "Organize this property?" and pin↔wiki sync

**Claim**: the suggestion's building-mirroring step creates child wikis "when the place already
has a community wiki"; manual sync matches building pins by footprint and non-building pins by
proximity; wikis auto-nest via place lineage; one wiki exists per place.

**Verdict: STALE (doc updated).** Three of the four hold exactly. The mirror-gating clause is
stale: `mirror_buildings_to_wiki()` was fixed to seed an invisible draft wiki when none exists
(`test_a_place_with_no_wiki_gains_a_draft_rather_than_nothing` documents the prior bug this closed
- "the mirror did nothing at all when the place had no wiki yet"), so buildings are no longer
silently dropped on a placeless pin. `docs/FEATURES.md` described the old, buggier gate. Doc
corrected.

## Property-records web-UI gate

**Claim**: the wiki's Ownership and Sale History cards gate `OFFICIAL`-sourced owner names/contact
details behind `SiteFeature.PROPERTY_OWNERS`; parcel/tax/assessment/district facts stay open;
private `PinOwner` notes and community-typed `WikiOwner` rows are never gated.

**Verdict: MATCHES.** Traced the full view→service→template path: the unfiltered queryset never
reaches template context, only `visible_owners()`/`sale_rows()`'s filtered output does - the same
shared functions the external API's (already-fixed) endpoints call, so there's no drift between
the two surfaces. `PinOwner` has no `source` field and is never gated; `WikiOwner.source=USER` rows
always pass through. Minor coverage gap noted, not a defect: only the Ownership panel has a
dedicated end-to-end HTTP-response-body test; Sale History's gating rests on a service-layer test
plus a template read, not its own live-response assertion.

## External-data caching and cost tracking

**Claim**: all external integrations are DB-backed-cached per-Location and rate-limited via
`ApiCallLog`/`ApiRateLimit`, toggled at `/site-admin/api-limits/`; `cost_estimate` can be null
(unpriced, not free); aggregated into a 30-day cost breakdown on the site-admin report *and* the
public `/costs/` page; an hourly background-enrichment task spends leftover rate-limit budget.

**Verdict: PARTIAL (doc updated).** Caching, rate-limiting, the toggle page, `cost_estimate`'s
null-means-unpriced semantics, and the background-enrichment budget mechanics (real leftover-budget
computation, not unconditional running) all match exactly. The doc overstated what `/costs/`
(public) shows: it's not a per-service breakdown there - that only exists on the site-admin page.
The public page blends a 30-day API figure into one combined monthly total with amortized
hardware/operating costs, trending over 12 months. Doc corrected to point at the site-admin page
for the per-service breakdown.

## Labels: kind count and shared-feature claims

**Claim**: one `Label` model with a `kind` backs four UI concepts (Tags/Categories/Statuses/
People); six features are shared across all four; seven named picker surfaces share one factory
and Sass mixin for consistent styling.

**Verdict: CONTRADICTS (doc updated).** The model is real, but there are five kinds, not four -
Media labels (photos/videos/documents, own "Organize" tab) were omitted entirely. Of the "shared"
features, bulk-convert and drag-to-reorder are deliberately, server-side restricted to
Tags/Categories/Statuses - `controllers/labels.py` explicitly guards against converting a People
label "via a crafted `kind` POST value." Only 3 of the 7 named picker surfaces actually use the
`createFilterPicker`/`createChipPicker` factories; the other 4 are separate bespoke
implementations, one of which doesn't even share the claimed Sass mixin (its own code comment:
"underline style, not pill buttons"). None of this looks like a bug - the restrictions are
deliberate and defended in code - so this was a doc-accuracy fix, not a code change: rewrote the
section to name all five kinds and scope the "shared" claims to what's actually shared.

## Custom Fields and External Photo Integrations

**Claim**: Custom Fields cover pins/photos/people/maps, private, in Settings → Advanced. Four
photo integrations (Immich, Google Photos, Flickr personal OAuth1, Flickr public no-OAuth import
capped at 100 photos).

**Verdict: MATCHES.** All four Custom Field targets exist and are genuinely private
(non-nullable owner FK, explicit "never shared" copy in the UI). Immich's API key is encrypted at
rest (`EncryptedTextField`, with a docstring justifying why); Google Photos is genuine OAuth;
Flickr personal supports exactly the three named import modes. The 100-photo cap on public album
import is enforced twice - once shaping the fetch, once again server-side at import time against a
freshly re-fetched album - so a tampered client request can't smuggle more than 100 photos past it.

## Account & Auth claims

**Claim**: login lockout after repeated failures; OAuth accounts can set a password to enable
new-device E2EE unlock; self-service deletion with a grace period and cancel; a fresh API key's
default grant is exactly `PROFILE_READ`/`PINS_READ`/`PINS_WRITE`/`PUSH_MANAGE`; keys are hashed and
revoke immediately.

**Verdict: MATCHES.** Two independent lockout dimensions (per-identifier and per-IP, both
cache-backed, admin-tunable thresholds). The OAuth password-unlock path is additive to the E2EE
model, not a downgrade - the recovery key remains the fallback, and a password reset correctly
invalidates only the password-wrap path. The 7-day deletion grace period is enforced by a real
Celery Beat chain and cancellation genuinely clears the pending state. The default PAT scope grant
matches exactly (not broader, not narrower); keys are salted-hashed, never plaintext; revocation is
checked fresh from the DB on every request with no caching/TTL delay.

## Trivia content-safety claims

**Claim**: the content classifier fails closed on AI unavailability; a submitter never learns their
question's fate, except a solo player's own unapproved question may rarely surface to them alone,
never in multiplayer; four moderation/generation/answer-check/wiki-incorporation toggles are
independent; a 10-minute stall sweep force-reveals abandoned rounds.

**Verdict: MATCHES.** Every branch of the classifier (disabled gateway, exception, unparseable
response) returns `approved=False`; only an exact approval token flips it true. The "surfaces to
the solo player only" leniency path is structurally gated on `participant_count == 1` - multiplayer
rounds draw candidates exclusively from `status=APPROVED`. The four `SiteSettings` toggles are
distinct fields; disabling AI generation still runs generated candidates through the
moderation-gated classifier rather than skipping it. The 10-minute sweep and `ABANDONED` marking
both confirmed against a real Celery Beat schedule entry.

## Consensus trust model

**Claim**: a Beta-Bernoulli trust posterior "weights how much a player's answer counts"; an
unsettled competitive-mode vote "lands in a cross-session tentative pool that later sessions can
confirm."

**Verdict: CONTRADICTS (doc updated, open question raised).** This is the one finding this round
worth flagging prominently: Consensus is the only game that writes back to shared wiki data, and
both halves of its trust story have a real gap.

The `trust_alpha`/`trust_beta` posterior is genuine and is actually consulted - for how often a
trust-check round gets injected, and for the confidence weight on a `FactEvidence` row (a separate
metadata layer consumed only by AI article-writing and Consensus's own recheck-round picker). It is
**never consulted in the path that actually changes the wiki**: solo answers apply instantly, and
competitive-mode answers apply the instant a plain one-vote-per-participant tally resolves - both
unconditionally, regardless of the submitter's trust score. A brand-new or actively-distrusted
account has exactly the same power to overwrite `Wiki.name`/`description`/etc. as a maximally
trusted veteran.

Separately, `ConsensusTentativeAnswer` (the promised path for an unsettled vote) accumulates
`support_count` across sessions correctly, but nothing anywhere - no controller, task, admin path,
or management command - ever promotes a row from `PENDING` to `APPLIED`/`DISMISSED`. It's a fully
modeled lifecycle nothing drives past its first state. An unsettled disagreement today just
accumulates support forever.

Doc corrected to describe actual behavior rather than the intended design. Not fixed in code -
implementing either (gating the direct write on trust, or building the tentative-promotion path)
is a real design decision, not a mechanical correction, and belongs to Jess - see below.

## Fixes applied (2026-08-25)

1. **`WikiOwnershipView`/`WikiPropertySalesView` leaked subscriber-only owner PII to any API
   key.** See [Property-owner subscriber gate](#property-owner-subscriber-gate). Fixed in
   `external_api/views_wiki.py`; 6 new tests in `test_external_api_property_owners.py`.
2. **`TripUndoHandler.restore()` could 500 instead of refusing cleanly.** See
   [Undo restore safety](#undo-restore-safety). Fixed in `services/undo/handlers/trip.py`; 3 new
   tests in `test_undo_restore_conflicts.py`.
3. **Three stale `docs/FEATURES.md` claims corrected** (not code changes): SpotGuessr eligibility
   caching, external API key scope, group-chat reaction support.
4. **External API's wiki alias/link deletion skipped the auto-removal tombstone.** See
   [Auto-source deletion permanence](#auto-source-deletion-permanence). Fixed in
   `external_api/views_wiki.py`; 4 new tests added to `test_auto_removals.py`.
5. **Floorplan GeoJSON features endpoint never resolved published (community) plans.** See
   [Floorplan personal-by-default scoping](#floorplan-personal-by-default-scoping). Fixed in
   `services/floorplans/resolution.py` (new `resolve_floorplan_row`) and
   `controllers/floorplans.py`; 5 new tests added to `test_floorplans.py`.
6. **Misleading `SiteFeature.NEARBY_RESEARCH` code comment** overclaimed which panels it gates.
   See [Nearby-research feature gate](#nearby-research-feature-gate). Comment-only fix in
   `models/subscriptions/model.py`.
7. **Two more stale `docs/FEATURES.md` claims corrected** (not code changes): PWYW
   dynamic-threshold's banked-access path and recompute cadence.
8. **Bulk pin-edit and the map's quick-edit dialog could remove a label without tombstoning it.**
   See [Pin/Place scope model and tombstones](#pin-place-scope-model-and-tombstones). Fixed in
   `controllers/pin_bulk.py` and `controllers/maps.py`; 6 new tests in `test_auto_removals.py`.
9. **Four more stale `docs/FEATURES.md` claims corrected** (not code changes): building-mirroring's
   community-wiki gate (removed, now seeds a draft), the public `/costs/` page's overstated
   per-service breakdown, the Labels section's kind count and shared-feature scope (rewritten), and
   the Consensus trust/tentative-pool description (rewritten to match actual behavior - see the
   open question below).

## Open questions for Jess

- **Consensus's trust posterior doesn't gate the live wiki write, and its tentative-answer
  promotion path is dead code.** The highest-priority open item from this round - see
  [Consensus trust model](#consensus-trust-model). Two independent design decisions: (1) should a
  low-trust account's Consensus answer actually be weighted or held back before it overwrites a
  live wiki field, the way the doc originally implied, or is instant-apply-then-let-trust-checks-
  catch-bad-actors-over-time the intended design? (2) should the `ConsensusTentativeAnswer`
  PENDING→APPLIED path actually be built (an unsettled vote currently just accumulates support
  forever with no resolution), or should the doc's "later sessions can confirm" promise be dropped
  since nothing was ever built for it?

- **Trip roster restore: refuse the whole trip, or silently drop the missing member?** The fix
  above chose "refuse" (consistent with `PinUndoHandler`'s own treatment of its FKs), but this is
  a product-UX call, not something the doc or existing code unambiguously settles - see the note
  at the end of [Undo restore safety](#undo-restore-safety).
- **Group-chat reactions: extend to the web UI, or leave API/mobile-only?** Currently a real gap
  between the two surfaces - either finish the parity or scope it as deliberate the way the other
  named DM/group-chat gaps already are.
- **Two stale in-code docstrings** (`external_api/__init__.py`, the `ApiKey` model's docstring)
  still describe the original 2-scope API key shape. Lower-stakes than the user-facing doc (a
  developer reading them has the real `ApiKeyScope` enum one file away) but worth a cleanup pass.
- **External API / OAuth2 token endpoint has no brute-force protection**, unlike the web login
  form - see [Login lockout coverage](#login-lockout-coverage) for why this wasn't fixed
  unilaterally. Worth a deliberate decision: is high-entropy-token-as-sole-defense the accepted
  posture (matching most API providers), or should a coarser per-IP request throttle be added
  regardless of success/failure, independent of a "lockout" tied to failure count?
- **`WikiOwner` `OFFICIAL` records can never be deleted through any surface** (noted in passing
  under [Auto-source deletion permanence](#auto-source-deletion-permanence)) - intentional today
  ("Official data can't be removed directly"), but worth confirming that's still the desired
  behavior now that the feature has matured.

## Not yet audited

`docs/FEATURES.md` is 893 lines; three rounds now cover twenty-five claims across Mapping & Pins,
External Data Enrichment, Labels, Custom Fields, External Photo Integrations, Account & Auth,
Trivia, Consensus, and the round-1/2 topics. Still untouched: Public Locations, Search &
Navigation, Lists & Saved Filters, most of Locations & Community Wiki, Building Floorplans (beyond
the round-2 personal/community-scoping fix), the Plugin System, Photos & Memories, Trips, Safety
Check-ins, Device Scanning (beyond retrieval privacy), the rest of Social Layer, the rest of
Notifications, the rest of Undo/Data Safety, the rest of Cost Tracking (Site Admin → Costs
add/edit/retire), Paid Subscriptions (beyond PWYW), Site Administration, AI Integration, REST API,
Direct Messaging, Real-time WebSockets, and SpotGuessr (beyond eligibility).
