# Mobile App Notes

Notes back to the mobile app team on `docs/notes/mobile_app_requirements.md`: what was already
there and the requirements doc got wrong, what shipped in this pass, and what was deliberately
declined or deferred (with the reasoning, so it can be argued with rather than just accepted).

Server v0.6.0+, branch `feature/external-api-mobile-v2`.

> **Scope of this pass: P0 and P1 only.** Every P2 parity-polish ask is **deferred to a later
> pass, not declined** — see Part 5 for the full list, which is long. Nothing in Parts 1–4
> describes work that is not on the branch.

---

## Part 1 — Corrections: asks that were already shipped

The requirements doc is dated 2026-07-27 and claims it "was fully reconciled against that
branch's actual serializers/views on 2026-07-27 — every item below is either a genuinely
still-open ask or a noted design question, not a stale guess."

That reconciliation missed the following. Each was re-verified by reading the code, not by
grepping for a guessed path. **Please update the app's `CONFIRMED ABSENT` comments** — several
of them are asserting the absence of things that are present and working.

### §6 [P0] "E2EE key endpoints are still 100% session-cookie-gated"

**This is the doc's single P0 blocker and it is false.** All eight E2EE views
(`enroll`, `keys`, `keys/{slug}`, `conversation-key`, `group-key`, `rewrap`, `rewrap-all`,
`reset`) extend `external_api.mixins.DualAuthJsonView` and declare per-method
`MESSAGES_READ`/`MESSAGES_WRITE` scopes. A PAT or OAuth2 bearer token has worked against them
for some time. "The server never sees plaintext" is not blocked and never was.

The real defect was narrower, and is the reason the grep came up empty: the routes live at
`/dashboard/e2ee/…`, and `external_api/schema.py::preprocess_external_api_only` filters the
published OpenAPI schema to paths starting `/dashboard/api/external/`. So the endpoints existed,
worked, and were **invisible in the very contract the app team was reading**. That is fixed —
the schema now admits the anchored `/dashboard/e2ee/` prefix, so the `@extend_schema`
descriptions already authored on those views finally render.

They are **not** being mirrored under `/api/external/v1/e2ee/`. The controller's module docstring
forbids duplicating that surface, and the reason is worth repeating: a drift between two copies
of a key-exchange contract means somebody's messages stop decrypting. One implementation, two
authentication paths, one published schema.

> Note the enrollment and rewrap endpoints require `current_password` **even for an OAuth2
> caller**. That is deliberate: a bearer token alone must never be sufficient to replace an
> account's key material.

### §8 [P2] "Field-locking isn't enforced at the API level yet"

False. The safety check-in PATCH delegates to `apply_checkin_edit`, which enforces every lock
under `select_for_update`, and there are already regression tests asserting it. Nothing was
changed here — "fixing" it would have meant breaking working code.

### §1 [P2] "Token introspection — no GET-scopes-and-expiry endpoint is confirmed"

`auth/session/` does **not** return `{user_id, session_key, csrf_token}`. It returns real
credential introspection: `credential_type`, `scopes`, `expires_at`, `issued_at`, `client_id`,
`name`, `user_uuid`. It is exactly the settings-screen endpoint §1 asked for. The only thing
genuinely missing is `last_used_at` (P2, deferred).

### §1 [P1] First-party OAuth2 client registration

Registered and provisioned by migration, via `provision_mobile_oauth_client` — public client, no
secret, PKCE required, authorization-code grant, token endpoint auth `none`.

**But the `client_id` is `urbanlens-mobile`, not `urbanlens-app`.** The app must change
`AppConfig.oauthClientId`. The server row is deliberately *not* being renamed to match the doc:
`client_id` is a published identifier, and renaming it would invalidate every token already
issued against it.

### §1 [P2] E2EE login-params

`/dashboard/e2ee/login-params/` is public JSON (`AllowAny`, no auth, no throttle) and stays that
way.

### §11 [P1] "Settings sync — no such endpoint; the existing `/settings/` is a different resource"

False, and this one is worth being precise about because the doc asks "which one is meant".
There is one settings resource, and `AccountSettingsView` + `services.profile_settings.SETTINGS_FIELDS`
already covers **every writable field on the website's own settings page** — privacy, style, map,
markup, AI, community and wiki-sync toggles included. `email` is excluded deliberately (changing
it is an identity operation with a verification flow, not a preference write).

Contact methods are the one genuine gap, and the cause is a *website* bug: an orphaned
`ContactMethodsForm` that no view renders. Exposing them is P2 and deferred — see Part 5.

One thing here **did** change, and it is a behaviour change worth reading: `PATCH
/profiles/{slug}/` used to accept `theme_mode`, `distance_units`, `community_enabled` and all
twelve `*_visibility` fields. It no longer does. Those are settings, writable via `PATCH
/settings/` behind `settings:write` — and a credential holding only `social:write` was able to
rewrite every privacy-visibility field on the account, which is exactly the surface
`settings:write` exists to protect. The profile endpoint now accepts `bio`, `area` and
`started_exploring` only, with a test asserting the two field sets never overlap again.

### §3 — list / saved-filter / label CRUD

Confirmed live and service-backed, as the doc assumed. Field names confirmed exactly:
`source_uuids`, `parent_uuids`, `pin_uuids`, `item_ids`, `source_saved_filter_uuid` — note these
are `*_uuids`, not `*_ids`.

### §13 — SpotGuessr stall handling and the rating-delta reward loop

Both already exist (beat sweep + `force_reveal_round`; `RatingChange` threaded end to end). The
game surface needed exposing, not rebuilding.

### Two field-level corrections

- **`rating` is not missing from pin PATCH.** A pin's rating is a *review*, written through
  `PUT`/`DELETE /pins/{slug}/review/`. It was never meant to be a scalar column on the pin
  update payload, and adding it there would have created two write paths to one value.
- **`address` and `visited` are derived, not stored.** `address` proxies the pin's `Location`;
  `visited` is `last_visited` plus a "Visited" status label. They are readable, and `visited` is
  now writable as sugar over the label operations — but they are not columns, which is why the
  original PATCH serializer omitted them. `address` stays read-only.

---

## Part 2 — Standing policy (unchanged from the previous pass)

### §1 Scope grant policy — PATs stay narrow

All the domain scopes (`lists:*`, `wiki:*`, `trips:*`, `photos:*`, `visits:*`, `social:*`,
`safety:*`, `messages:*`, `notifications:*`, `search:read`, and now `games:*`) exist and are
enforced per-endpoint. `_default_api_key_scopes()` was **deliberately not widened** and will not
be: silently expanding every already-issued PAT's grant is a privilege escalation, not a
convenience.

Consequences the app should design around:

- **OAuth2 + PKCE is the app's real path.** The consent screen lets the client request exactly
  the scopes it needs from the full vocabulary. This works today.
- **PAT-style `ulk_…` keys get four scopes only** (`profile:read`, `pins:read`, `pins:write`,
  `push:manage`) and there is no scope-picker UI. A pasted personal key will not reach the newer
  domains. That is not an oversight to route around.
- **`messages:read`/`messages:write` are unreachable by any PAT**, even one that somehow carries
  them (`OAUTH2_ONLY_SCOPES`, enforced in `external_api/permissions.py`). Encrypted-DM transport
  is OAuth2-only by design, because a PAT has no consent step to show the user what is being
  requested, and API keys end up in CI configs and screenshots.

`games:read`/`games:write` were added this pass on the same terms — not in the default PAT grant,
not OAuth2-only.

### §4/§6 Markup-map share attachments bypass share provenance (still deferred)

Attaching a `MarkupMap` to a direct message or a wiki records no `LocationExposure`, unlike
attaching a *pin*. Pre-existing; the web composer has always behaved this way, and the API
endpoints match existing behavior rather than diverging. Still unfixed because it needs a
decision on whether a hand-drawn annotation with no linked pin should count as a location
exposure at all. Tracked in `docs/PROBLEMS.md`.

### §4 Wiki edit: the external API is strict, the internal HTMX view stays lenient

`PATCH /wikis/{slug}/` rejects an invalid `security` value or malformed date with a hard 400.
The internal HTMX view still silently drops the bad field and returns `{"ok": true}` — migrating
it needs field-level error rendering in the About card, which is UI work. Tracked in
`docs/PROBLEMS.md`; it does not affect the API contract.

### §2 `parent_id` on pin create

`POST /pins/` accepts an optional `parent_id` (uuid of one of the caller's own pins). Without it,
coordinate resolution uses a 50m fuzzy-dedup radius matching the map UI's top-level "Add pin"
flow. With it, resolution switches to exact-coordinate matching, mirroring the map UI's own
detail-pin flow — which is what makes it possible to create a genuine child pin (a building's
north entrance, ~10m from its main pin) that the fuzzy radius would otherwise reject as a
duplicate. The create response includes `parent_uuid` (null for a top-level pin).

---

## Part 3 — Decisions on the asks that were not built as requested

Each of these was implementable in some form; the question in every case was *which* form. The
recommendation and its reasoning are given so they can be challenged.

### D1 — §2 Pin note threading and reactions → **declined**

`PinNote` has two fields (`text`, `pin`) and is documented as an append-only private log. There
is no internal implementation of note threading or note reactions anywhere, so this is "build a
feature", not "expose one" — it needs a migration and a fourth nullable `Reaction` host FK, and
`PinNote.Meta.ordering` stops being a valid tree ordering the moment notes nest.

More to the point, the capability the app wants already exists on the right model: **pin comments
accept `parent_id` externally today**, and comment reactions shipped this pass. A pin note is
single-author and private — a reaction on one would have no audience.

**Point threading and reaction clients at `/pins/{slug}/comments/`.** The `PinNoteSerializer`
docstring now says so explicitly.

### D2 — §3 Global label ordering → **fail closed, and a live bug fixed on the way**

While auditing this: `OrganizePrioritySaveView` validates submitted label ids against
`visible_to()` — which includes *global* labels — and then updates `Label` rows with **no profile
scoping**. Every user dragging their Display Order tab was rewriting `order` on shared global
labels site-wide. That is fixed, with a regression test asserting a global label's order survives
another user's reorder.

The website's Display Order save now scopes its write to labels the caller **owns**, skips any
global in the submission, and returns `skipped_global_ids` so the UI can render those rows as
position-locked rather than silently losing the drag. Per-profile ordering of global labels
would need `LabelCustomization.order`; that is a tracked follow-up.

The REST label-reorder endpoint itself is P2 and deferred — but it must adopt the same rule
when it ships, which is why the decision is recorded here rather than left to be rediscovered.

Expect one support question: the (previously bugged) web UI looked like it let you reorder
globals. It no longer does, because it never really did — the reorder was being applied to
everyone.

### D3 — §6 Per-conversation disappearing messages → **blocked on a product decision**

Answering the direct question: **no per-thread retention field exists anywhere.** Retention is
account-wide (`Profile.direct_message_delete_after`) and snapshotted onto each message at send
time.

Two things have to be decided before this can be built, and neither is an engineering call:

1. **Whose setting is it?** In this codebase retention is *sender*-controlled — the sender keeps
   their own copy indefinitely and controls when the *recipient* loses the message. A
   per-conversation version of that is per-(sender, peer), which is **not** the mutual
   "disappearing messages" mode Signal and WhatsApp users expect. Shipping the former under the
   latter's name is a privacy misrepresentation, not a naming quibble.
2. **Is it retroactive?** The existing per-message snapshot design says no.

**Recommendation if it proceeds:** a sender-side per-peer override with non-retroactive
semantics, named "retention" rather than "disappearing messages", via a new
`ConversationRetention` model read only at send time. Group chats have no retention field at all
and would be a separate piece of work.

### D4 — §8 Live location → **design settled, shipped in the third pass**

The doc deferred this as needing "its own privacy-scoped design". The design question was really
one thing: should a long-lived PAT be able to read a continuously-updating precise position, or
does that need an expiring OAuth2 token?

**Answer: existing `safety:read`/`safety:write`, no new scope and no credential-kind gate.** The
position is already readable by the same partner's browser session, so a scope gate would be
theatre. The real containment is structural — reads belong on a *dedicated* endpoint, with the
fields kept out of the check-in summary and detail serializers, so a `safety:read` credential
that merely syncs check-ins never incidentally accumulates a position history the model
deliberately refuses to store.

Two things must ship with it: its own throttle bucket (the standard 300/hour write cap dies in
under an hour at one fix per ten seconds), and the residual risk stated plainly in both the
endpoint description and the key-scope copy — **a leaked PAT holding `safety:read` becomes a
live tracker for every check-in its holder partners on.**

Shipped: `GET/PATCH /safety/checkins/{slug}/location/` — see Part 7 for the endpoint shape.

### D5 — §9 `display_name` → **first/last name yes, `username` no**

There is no `display_name` field. `Profile.username`/`first_name`/`last_name` are read-through
properties onto `User`, and every `display_name` on the wire resolves to `username` through
`resolve_visible_identity`.

**Recommendation: expose `first_name`, `last_name` and the six contact methods** — no
uniqueness constraints, no side effects. Shipped in the third pass, folded into `GET/PATCH
/settings/` rather than a new endpoint — see Part 7 for why.

**`username` stays off the external surface** regardless: changing it is a uniqueness-checked
identity mutation, and `Profile.slug`
is derived from username *once and never regenerated*, so a rename would leave every cached
`profiles/{slug}/` URL stale. Allowing it needs an explicit `regenerate_slug` decision plus a
stale-slug redirect story — a separate piece of work, not a serializer field.

### D6 — §9 Friend mute → **it was a data-integrity bug, now a real flag**

Confirmed behaviour of the old endpoint: `POST /friends/{uuid}/mute/` ignored the body entirely,
was not a toggle, had no unmute, and returned a serializer containing neither `profile_uuid` nor
`is_muted` (the app was looking at the *messaging* `is_muted`, an unrelated mechanism).

The deeper problem: mute was implemented as a Friendship **status**. Muting an accepted friend
overwrote `Accepted`, so `Profile.are_friends` stopped treating them as friends for every
visibility gate downstream — and that is also why the website's own Unmute button returned 400.

**Fixed properly:** `Friendship.muted` is now a boolean set without touching `status`, `is_muted`
is on the serializer (exactly what the app already expected), and the write is an
explicit-target `PATCH {"is_muted": bool}` rather than a toggle — a retried POST over a flaky
mobile link must not silently invert state. The bodyless `POST` remains as a deprecated alias for
`true`.

Existing `status='Muted'` rows carried no record of their prior state; the data migration's
choice and its rationale are documented in the migration itself and in `docs/PROBLEMS.md`.

**Read this before building a mute toggle in the app.** Splitting mute off `status` fixed the
un-friending bug, but it exposed a second one it did not fix: *friendship-level mute has never
actually suppressed anything*. No notification delivery path consults the flag — grep `muted`
across the four notification services and there is no hit. So the website's Mute button and this
endpoint both record a preference nothing honours; the muter still gets friend-request,
pin-share, trip-invite and safety notifications from that profile. The flag is stored faithfully
and the API reports it honestly, but **a UI that promises silence would be lying** until delivery
is wired up (tracked in `docs/PROBLEMS.md`, with the recommended shape: one `is_muted_by` helper
consulted where `NotificationLog` rows are created, so it cannot be forgotten per notification
type). The two mute mechanisms that *do* work are unrelated models: `DirectMessageMute`
(per-sender DM mute) and per-group chat mute — both reachable from this API.

Also note the flag is a property of the shared row joining the pair, not of the viewer. Label it
"muted", not "muted by you".

### D7 — §11 Immich / Flickr / Google Photos → **split by feasibility; both halves deferred (P2)**

Immich is `server_url` + `api_key` + a ping, fully expressible as JSON — connect, disconnect,
status and scan are all straightforwardly buildable whenever this is picked up.

Flickr (OAuth1) and Google Photos (OAuth2) are not. Both redirect through callbacks that are
`LoginRequiredMixin` and bind state to the **session's** profile id, which a bearer client cannot
satisfy. Status and disconnect would be fine; *connect* is the genuinely blocked part.

A native app *can* open a system browser, so this is not impossible. **Recommendation:** relax
both callbacks to accept a credential-signed, short-lived, single-use state instead of requiring
a session — but note what that costs: it removes the session as a second factor and turns a
stolen callback URL into an account-linking attack. That trade needs a decision, not an
implementation. The Google Photos **Picker** is separately browser-bound and is out of scope for
v1 rather than being approximated.

### D8 — §12/§13 Panel imagery and game round images → **never inline base64**

Satellite and Street View slides carry base64 `data:` URIs (Google Static Maps, Mapbox, Bing and
Street View are all fetched server-side). Several providers × five slides is plausibly **5–15 MB
in a single JSON response**, and the throttle counts requests, not bytes.

`satellite` and `street_view` are therefore **excluded from JSON exposure** until a signed
slide-image proxy exists (same `Signer` pattern as the existing media proxy, served through the
credential-or-session media mixin, requiring `media:read`). `PanelSource.api_payload` now exists
and returns `None` for them, with a comment saying why — the exclusion is enforced in code, not
just written down here.

(The generic panel listing/fetch endpoints themselves shipped in the second pass — see Part 6.
The `api_payload` interface they build on shipped here, as did the two panel *security* fixes —
see Part 4.)

SpotGuessr round images are served as **EXIF-stripped bytes** from a dedicated endpoint under
`{games:read, media:read}` — which also closes a residual answer leak, since a stored JPEG may
retain EXIF GPS pointing straight at the answer.

Related, logged in `docs/PROBLEMS.md`: the imagery render path re-runs the full provider chain on
the request thread even when the panel is "ready".

### D9 — §12 Upstream cost attribution → **attribute yes, budget no**

Per-service rate limiting and cost estimation already work and already apply to external callers
with no new code, because the upstream call always happens inside the Celery task.

What is missing is **attribution**: `ApiCallLog` has no profile, user, credential or pin FK, so
"how much upstream cost did this API key cause" is currently unanswerable — which the project's
own per-call cost-tracking rule requires.

**Recommendation:** add nullable `profile` and `origin` (`web|external_api|enrichment|system`)
columns and thread the triggering profile through `schedule_panel_fetch → fetch_panel_source →
run_panel_fetch`. Deferred to its own pass rather than bolted onto this one, because a
per-credential cost *budget* needs a product answer for what happens on exhaustion, and the
honest limitation needs documenting: fetches are Location-scoped and single-flight, so
attribution records **who paid, not who benefited**. Scope the FK to profile id only — never pin
or coordinates, which would make the log a location-research signal in its own right.

### D10 — §11 CORS → **browser-origin third-party clients are out of scope for v1**

`CORS_ALLOWED_ORIGINS` admits only urbanlens.org and localhost, and django-cors-headers applies
one policy site-wide. Native clients are unaffected (no Origin enforcement in a native stack); a
browser-based third-party integration cannot call this API at all.

**That limitation is being documented rather than removed.** A bearer-token API that sends
`Authorization` and relies on no cookies gains nothing from permissive CORS, and loosening
`CORS_ALLOWED_ORIGINS` simultaneously loosens `CSRF_TRUSTED_ORIGINS` — which the session branch
of `DualAuthJsonView` actively depends on. If browser clients later become a requirement, the
correct shape is a `CorsMiddleware` subclass scoped to the `/dashboard/api/external/v1/` prefix,
not a wider global allowlist.

### D11 — §13 Game scopes → **`games:read` / `games:write`**

Rejected alternatives, for the record:

- **Reusing `pins:*`** — playing a game must not require pin *delete* authority, and the consent
  screen would read as "let this app rewrite my map".
- **A single `games:play`** — the throttle classifier derives the read-vs-write tier from the
  `:read`/`:write`/`:manage` suffix, so the expensive start/guess POSTs would land in the loose
  hourly *read* budget.

The pair matches the documented `domain:action` convention, auto-classifies correctly, and covers
Trivia and Consensus later without minting further scopes.

### D12 — §5 Trip → Google Calendar export → **achievable, and shipped**

The doc assumed export was browser-bound. It is not: tokens are stored per-user with a refresh
token and the gateway auto-refreshes, so only the **one-time consent** is browser-bound. Export
and unexport are now real JSON endpoints.

When the user has never connected a Google account, the endpoint returns **409
`calendar_not_connected`** carrying an `authorization_url` — and that URL is the *site's own*
connect route, not a Google URL. A Google authorization URL minted by the API would 302 to the
login page and lose the `code`; the app should open the site route in a system browser and retry.

### D13 — Wiki detail-pins / child-wiki CRUD → **declined, like D1**

There is no `POST /wikis/` anywhere internally — a wiki is only ever created as a side effect of
pin creation, or the web "Create Community Wiki" button, both of which go through the same
pin-sync bridge. A child wiki also carries a real extra constraint the requirements doc didn't
account for: it needs its own distinct `Location`, never shared with its parent's. Building general
wiki creation from scratch was not asked for by anyone and is out of scope here; the existing
pin-sync bridge (child pins → child wikis) remains the real, working mechanism for this.

---

## Part 4 — Convention notes for the app

- The requirements doc's summary of JSON conventions is accurate. One addition: **the error
  envelope is now uniform across the whole external API** — `{"error": "..."}`, and
  `{"error": "Invalid request.", "fields": {"name": ["..."]}}` for field-level validation
  failures. Previously three shapes were reachable (`{"error"}` from hand-written returns,
  `{"detail"}` from uncaught 404s, and a bare field-keyed dict from serializer validation). If
  the app parses `detail` anywhere against this API, that path is now dead.
- **404, never 403**, for anything whose existence the caller has not already been shown. This is
  deliberate and pervasive: a 403 confirms the object exists. The handful of genuine exceptions
  (non-organizer trip settings, deleting someone else's group message) are cases where the caller
  was *already* shown the object, so a 403 leaks nothing — and each carries a comment saying so,
  to stop a future reviewer "fixing" it into a 404.
- Pagination is page-number style almost everywhere, with the pin/tombstone sync feeds cursor-based
  and a couple of small envelopes (a trip's map markers, the undo feed) non-paginated by design.
  The two normalizations flagged in the previous revision of this note — the memories journal's
  bespoke `{entries,total,omitted_sources}` shape, and the safety-maps endpoint's bare top-level
  array — are now fixed (see Part 7). Both moved onto the standard `{count,next,previous,results}`
  envelope; the journal kept its `omitted_sources` field alongside it, and switched from
  `limit`/`offset` query params to the usual `page`/`page_size`.
- **Versioning discipline:** v1 changes additively. Anything breaking mints `/v2/` and serves a
  `Sunset` header on v1.
- **Scope your credential properly for sockets.** WebSocket connections now enforce scopes, which
  they previously did not: `ws/messages/` needs `messages:read` to connect and `messages:write`
  to send, `ws/notifications/` needs `notifications:read`, the safety chat needs `safety:*`, and
  the game sockets need `games:*`. Since no PAT carries those by default, **an existing personal
  access token can no longer open them at all** — use OAuth2, which is the app's primary flow
  anyway. Revoking a credential now also terminates its live sockets rather than leaving them
  streaming until they disconnect.

---

## Part 5 — What shipped, and what is deferred

### Shipped this pass (P0 + P1)

**Pins**
- `PATCH /pins/{slug}/` now writes `description`, `color`, `pin_type`, `priority`, `danger`,
  `vulnerability`, the three `date_*` fields, `security`, `label_uuids` and `visited`. Previously
  all of these were **silently dropped while the endpoint returned 200** — an edit that appeared
  to succeed and never existed. `label_uuids` is a full replacement and each removal writes a
  `PinAutoRemoval` tombstone, so auto-tagging cannot quietly put a removed label back.
  Note `priority`/`danger`/`vulnerability` publish a community `WikiStatVote` — a real
  consequence of a seemingly private edit, documented on the endpoint.
- `PUT|DELETE /pins/{slug}/comments/{id}/reactions/{emoji}/`
- `GET|PUT /pins/{slug}/article/` + revisions list/detail/restore (scoped `pins:*`, never
  `wiki:*` — a pin article is private owner content)
- `POST /pins/{slug}/wiki-sync/push/` and `.../pull/`
- `POST /pin-shares/{id}/respond/` — accept or reject a pin shared into a message

**Safety**
- `GET|POST /safety/checkins/{slug}/messages/` — the owner's side of the check-in chat, which had
  no REST surface at all
- `GET /safety/partner-invites/`, `POST .../{uuid}/accept/`, `POST .../{uuid}/decline/`
- `GET /safety/partner-checkins/`, `GET .../{uuid}/`, `POST .../{uuid}/mark-safe/`

**Messaging**
- `POST /messages/groups/{uuid}/messages/{id}/react/`
- `DELETE /messages/groups/{uuid}/messages/{id}/`
- `POST /messages/groups/{uuid}/leave/`
- `PUT|DELETE /messages/{peer_slug}/mute/` and `/messages/groups/{uuid}/mute/`
- `whoami/` now returns `slug` as well as `uuid` — without it a client cannot tell which
  messages in a thread are its own, since every payload identifies authors by slug

**Social**
- `POST /friends/{uuid}/unblock/` (and the underlying escalation fixed — see below)
- `PUT|DELETE /profiles/{slug}/avatar/`, `POST /profiles/{slug}/avatar/emoji/`
- `GET /profiles/{slug}/annotations/`, `PUT|DELETE .../nickname/`, `PUT|DELETE .../trust/`
- `PATCH /friends/{uuid}/mute/` with an explicit `{"is_muted": bool}`, plus `is_muted` on the
  friendship payload

**Trips**
- `PATCH /trips/{slug}/settings/` — the four permission fields had no write path anywhere but the
  website's own HTMX form
- `POST|DELETE /trips/{slug}/calendar/` — Google Calendar export and unexport, plus
  `account_email` on the calendar block

**Wikis / search / games**
- `POST /wikis/{slug}/aliases/{id}/use/`, plus `is_current` on the alias payload
- `GET /search/?q=` — global search, with **per-provider scope gating**: sections the credential
  cannot read are dropped and named in `omitted_types` rather than 403-ing the call
- SpotGuessr solo play: bootstrap, session create/list/detail/summary, current round, guess
  submission, and EXIF-stripped round images

**Cross-cutting**
- One uniform error envelope across the package
- E2EE endpoints now appear in the published OpenAPI schema (they always worked; they were
  invisible)
- Routing split into per-domain modules, ordered by specificity so a new route cannot be
  silently shadowed by an existing generic slug

### Security fixes found while building (nobody asked for these)

1. **WebSocket credentials enforced no scopes at all.** A `pins:read` key could open and post in
   a safety-check-in chat, and a PAT could open `ws/messages/` despite `messages:*` being
   OAuth2-only on every HTTP route. Revoked credentials also kept live sockets open.
2. **Anyone could clear a block placed on them.** The friendship lookup is direction-agnostic and
   the delete path did no ownership check, so `DELETE /friends/{blocker_uuid}/` lifted someone
   else's block. Present on the website too. A second, deeper defect had to be fixed for the
   guard to be sound: `block_profile` reused whatever row already joined the pair, so a block
   placed on an *inbound friend request* left the row pointing at the blocked party.
   **Caveat:** rows blocked before this fix carry no signal to repair them. For such a row the
   effect is inverted — the true blocker gets a 404 and must re-block to normalize it. Recommend
   an audit query over pre-deploy `status="Blocked"` rows; a `blocked_by` FK is the clean fix.
3. **Display Order reordering wrote across tenants.** Validation admitted global labels
   (`visible_to` spans them by design) and the write was unscoped, so every user's drag renumbered
   shared labels site-wide.
4. **A subscription-gated research panel was UI-gated only** — fetchable directly by any
   logged-in user.
5. **SpotGuessr leaked the answer before the reveal** via an EXIF-derived `image_caption` the web
   client happens not to render. The external round serializer now copies an explicit whitelist
   rather than forwarding the internal payload, so the next added field cannot repeat it.
6. **Declining a safety-partner invite did not revoke live access.** The chat consumer only checks
   permission at connect time, so a partner who resigned kept streaming the owner's live position.
7. Wiki comment reactions sent no notification (a controller duplicated the service's toggle);
   the internal alias-rename wrote junk `{"from": "X", "to": "X"}` history rows.

### Deferred to a later pass (P2 — not declined)

Pins: pin-note threading (declined outright, see D1). Wikis: ownership and sale history **write**
side (read shipped, see Part 7), public-vote ballots, detail-pins CRUD (declined, see D13).
Messaging: per-conversation retention (blocked on product, see D3). Misc: Immich/Flickr/Google
Photos connections.

**Shipped in the second pass, no longer on this list** (see Part 6): custom field definitions +
photo values, undo, enrichment panels as JSON + generic panel listing/fetch, AI assistant.

**Shipped in the third pass, no longer on this list** (see Part 7): the memories-journal and
safety-maps pagination normalizations, the styled OAuth consent screen; pin bulk
operations/visit `PATCH`/address components; label REST reorder + bulk delete/edit/convert + list
markup maps (list-level bulk delete/edit scoped down, see Part 7); safety live location (D4);
`first_name`/`last_name` + contact methods on `/settings/` and the public social-links endpoint
(D5, partial — see Part 7 for what's still open); wiki boundary set/clear, cover-photo write, alias
nickname toggle, article-revision hard-delete, and ownership/sale-history **read** (write deferred,
see Part 7); memories timeline and on-this-day; the new-pin suggestions review queue
(`GET/POST /suggestions/pins/...`, accept applies suggestion defaults only — see Part 7); messaging
`image_uuids` attachments, additive alongside `image_ids` (see Part 7); SpotGuessr round-timer
expire, photo feedback, preferences write, and the eligible-count/eligible-pins pre-check pair (see
Part 7 — **SpotGuessr only**, not Trivia/Consensus: see Part 7 for why).

**Off this list entirely, not just deferred** (see Part 6): site config/admin endpoints (will not
be exposed to the mobile app), backup export/import (not for the mobile app at this time).

---

## Part 6 — Second pass (2026-07-28): Custom Fields, Panels, Undo, AI Assistant

Four of the seven items Part 5 listed as P2-deferred. **Not** in this pass: Site Config, Connections
(Immich/Flickr/Google Photos), and Tools (backup export/import).

### Site Config and Import/Export — declined for mobile, not deferred

Two of the three remaining items just became decisions rather than open questions:

- **Site config/admin endpoints will not be exposed to the mobile app.** `urls_site.py` stays an
  empty placeholder by design. If a future need surfaces (announcements, version/health), it gets
  scoped and decided then — nothing here is scaffolded in anticipation of it.
- **Import/export (KML/GPX/CSV) will not be built for the mobile app at this time.** Possible in
  the distant future; not scheduled, not scaffolded.

**Connections (Immich/Flickr/Google Photos) is still genuinely open** — unchanged from D7. The
Flickr/Google Photos OAuth-callback question is being assessed independently of this API work, and
nothing in this pass touches it.

### Custom Fields

`GET/POST /custom-fields/`, `PATCH/DELETE /custom-fields/{id}/`, `GET /photos/{uuid}/custom-fields/`,
`PUT/DELETE /photos/{uuid}/custom-fields/{field_id}/` — see [Custom Fields](../EXTERNAL_API.md#custom-fields).
New scopes `custom_fields:read`/`custom_fields:write`, additionally requiring `photos:read`/
`photos:write` on the two photo-scoped routes (a field *value* is photo data — a credential scoped
for custom fields but not photos shouldn't incidentally read photo-attached data). `reference`-type
fields are accepted on create/list but not yet writable as a value; that needs its own
target-resolution design.

### Undo

`GET /undo/`, `POST /undo/{uuid}/restore/` — see [Undo History](../EXTERNAL_API.md#undo-history).
New scopes `undo:read`/`undo:write`. Routed under `urls_tools.py` (a utility belonging to no single
resource), not a new `urls_*` module.

### Panels

`GET /pins/{slug}/panels/`, `GET /pins/{slug}/panels/{key}/` — see [Panels](../EXTERNAL_API.md#panels).
New scope `panels:read`. `satellite`/`street_view` stay excluded per D8, unchanged.

**A latent exposure was found and closed while building this, not requested by anyone:**
`PanelSource.api_kinds` defaults to non-empty on the two most common plugin base classes
(`InfoPanelSource`, `GalleryMediaSource`), so every plugin built on them was already implicitly
"on the API" the moment this endpoint existed — opting in was the default, not opting out. Five
built-in plugins got an explicit `api_kinds = frozenset()` added before this shipped:
`property_records` and `loopnet` (real-estate data with licensing concerns), `yelp` and
`google_places`/`google_images` (third-party ToS on redistribution, and in two cases the photos
only ever resolved through an internal session-authenticated proxy anyway, so exposing them here
would have been useless as well as risky). EPA ECHO's two panels were reviewed and left alone
deliberately: the nearby-facilities list is already gated behind `NEARBY_RESEARCH` (which this
endpoint honors the same way the web tab strip does), and the exact-site compliance card is
intentionally public government data.

### AI Assistant

`POST /assistant/message/`, `POST /assistant/reset/` — see [AI Assistant](../EXTERNAL_API.md#ai-assistant).
New scope `assistant:write`. Stateless: the client carries `history` in the request/response body
rather than a session, since a bearer-token client has no session to keep it in — `run_assistant_turn`
already took `history` as a plain argument, so nothing changed at the service layer to support this.

**Bug fixed on the way (benefits the existing web chat too):** `run_assistant_turn` never called
`log_api_call` despite invoking the model gateway up to `MAX_TOOL_CALLS` (6) times per turn — every
other gateway-backed feature in this codebase logs its cost per call, this one silently didn't. One
call now covers the whole turn (the gateway accumulates cost across every `send_prompt()` on the
same instance, so a single post-loop read is correct regardless of how many tool round-trips
happened), in a `finally` so it fires whether the turn succeeds, fails, or hits the action limit.

---

## Part 7 — Third pass (2026-07-28): pagination envelopes, styled consent screen, P2 backend items

### Memories journal and safety-maps pagination envelopes

Both normalized onto the standard `{count, next, previous, results}` envelope:

- `GET /memories/journal/` — dropped the bespoke `{entries, total, omitted_sources}` shape and the
  `limit`/`offset` query params. Now `page`/`page_size` like every other list endpoint, `results`
  instead of `entries`, `count` instead of `total`. `omitted_sources` is kept as an extra top-level
  field alongside the standard four — same pattern as global search's `omitted_types`.
- `GET/POST /safety/checkins/{slug}/maps/` — the bare top-level array is now the standard envelope.
  A check-in's map list is realistically tiny (a primary route plus a handful of references), so
  `next`/`previous` are almost always null in practice, but the shape now matches every other list
  endpoint rather than being a documented exception to it.

Both were genuinely additive-unsafe before this: a bare array or a one-off key set can't gain a
field later without a client that assumed the old shape breaking. Fixed now, before either endpoint
has a real client depending on the old response.

### Styled OAuth2 consent screen

`/oauth/authorize/` now renders through the site's own themed auth shell
(`dashboard/themes/auth_base.html`) instead of django-oauth-toolkit's bundled default, which pulled
an unstyled Bootstrap 2 stylesheet off a dead CDN link (`netdna.bootstrapcdn.com`). This is the one
user-visible gate before a client — including this project's own first-party mobile app — is
granted a scope like `messages:*` against an E2EE mailbox, so it reading as a generic framework
page rather than UrbanLens was a real trust signal, not just cosmetic.

Only `oauth2_provider/authorize.html` was overridden (`src/urbanlens/dashboard/templates/oauth2_provider/`,
picked up ahead of the toolkit's own bundled template because `TEMPLATES["DIRS"]` is searched
before the app-directories loader). The error branch (`{% if error %}`) is themed too. The
authorize page links to `oauth2_provider:authorized-token-list` (where a user revokes a connected
app's access) — that page, and the rest of the toolkit's application-management views, still render
with the unstyled default; restyling those wasn't asked for and is a separate, smaller follow-up if
it's ever wanted.

### P2 backend items — in progress

The remaining P2 catalog from Part 5 is being picked up in this pass, **except** the three items
with a standing reason not to: pin-note threading (D1, declined outright, not deferred),
per-conversation message retention (D3, blocked on a product decision about whose setting
retention is), and Immich/Flickr/Google Photos connections (D7 — the Flickr/Google Photos
OAuth-callback question is being assessed independently by the project owner and is explicitly out
of scope here). Progress and shipped-summary to follow as each domain lands.

#### Shipped so far this pass

**Pins** — `POST /pins/bulk/delete|merge|edit/` (thin wrappers over the map toolbar's existing
multi-select logic, restored via the generic `/undo/{uuid}/restore/` rather than a bulk-specific
undo route); `PATCH /pins/{slug}/visits/{visit_id}/` alongside the existing GET/POST/DELETE;
read-only address components (`city,state,county,country,zipcode`) added to pin detail.

**Lists & Labels** — `POST /labels/reorder/` (mirrors the internal drag-reorder view's D2 rule:
validates against every visible label, writes only to owned ones, reports `skipped_global_uuids`);
`POST /labels/bulk/delete|edit|convert/`; `POST /lists/{slug}/markup-map/`. **List-level bulk
delete/edit was scoped down** — the research pass found only item-level list operations
internally, no list-level bulk action to wrap, so none was invented; noted here rather than
silently dropped.

**Safety live location (D4)** — `GET/PATCH /safety/checkins/{slug}/location/`, its own endpoint
excluded from the check-in read/write bodies, viewer-scoped GET (owner or ACCEPTED partner,
matching the chat consumer) and owner-only PATCH, own throttle bucket. See D4 above and
[Safety](../EXTERNAL_API.md#safety) in the reference doc for the full shape.

**Social (D5, partial)** — `first_name`/`last_name` and the six `ContactMethodsForm` fields
(`phone_number,signal_username,discord_username,whatsapp_number,telegram_username,matrix_handle`)
added to `GET/PATCH /settings/`, **not** a new endpoint. This needed one real design call:
`ProfileUpdateSerializer` (`PATCH /profiles/{slug}/`, `social:write`) is deliberately walled off
from every settings-shaped field — that's the fix from the `social:write`-privacy-escalation bug
noted under §9 above — and `ContactMethodsForm`'s six fields are exactly settings-shaped (private,
no public-presentation role), so extending `/settings/` was the only option consistent with that
boundary; `ProfileSettingsOverlapTests` keeps enforcing it. `first_name`/`last_name` are a`User`
passthrough with no `Profile` column, so `apply_settings_patch` now saves `user` directly when
either is touched, alongside its usual `profile` `update_fields` return for the caller.
`GET/PUT /profiles/{slug}/social-links/` also shipped — the *public* link list
(Instagram/Bluesky/Discord/UER/Facebook/Flickr/YouTube/TikTok/Reddit/website), which is a
different resource from the private `discord_username` contact field above and from
`ContactMethodsForm`'s own separate Discord entry; the two Discord values can legitimately differ.
PUT is a full replace, matching `/safety/contacts/`'s own precedent. One real bug found and fixed
during testing: the first `website`-platform validator prepended `https://` to a scheme-less
handle *before* checking the raw scheme, so `javascript:alert(1)` sailed through disguised as
`https://javascript:alert(1)` (hostname parses as the harmless-looking `javascript`) — fixed by
checking the raw scheme first, mirroring `parse_social_link`'s own two-step guard.

**Wikis (D13 addendum + boundary/cover-photo/ownership)** — `GET/POST /wikis/{slug}/boundary/`
(thin wrapper over `controllers.boundary.WikiBoundaryView`'s resolution chain and
pending/refreshing polling contract); `PUT/DELETE /wikis/{slug}/cover-photo/` (wraps
`controllers.image_gallery.WikiCoverPhotoView`, photo must already be in the wiki's gallery);
`POST /wikis/{slug}/aliases/{id}/toggle-nickname/` (wraps `WikiAlias.toggle_nickname()`, no edit-
history entry, matching the internal `LocationAliasToggleNicknameView` it mirrors);
`DELETE /wikis/{slug}/article/revisions/{id}/` (self-service, author-only — see the reference doc);
`GET /wikis/{slug}/ownership/` and `GET /wikis/{slug}/sales/` (new, read-only — `WikiOwner`/
`WikiPropertySale` had full models/querysets but no controller at all internally).

The plan's original text said Ownership/Sales should be read-only "since no write UI exists to
mirror" — that turned out to be **wrong**: `controllers/property_owner.py` has a complete
`WikiOwnershipPanelView`/`WikiOwnerUpdateView`/`WikiOwnerRemoveView`/`WikiPropertySaleTabView`/
`WikiPropertySaleDeleteView` stack (owner dedup by case-insensitive name, M2M owner↔location
linking, OFFICIAL-source write protection). Shipped read-only anyway rather than silently
expanding this phase's scope to a full CRUD surface with its own dedup/linking/protection design —
recorded here per the plan's own instruction not to let a scoping-down go undocumented. The write
side is a reasonable candidate for a future pass; it is a real gap, not a deliberate decision that
writes shouldn't exist.

Wiki "detail-pins CRUD" is confirmed declined per **D13** in the Decisions Recap below — no
`POST /wikis/` exists anywhere internally (wikis are only ever created as a side effect of pin
creation), and a child wiki needs its own distinct `Location`, never shared with its parent; the
existing pin-sync bridge (child pins → child wikis) remains the real mechanism for this.

**Photos/Memories** — `GET /memories/timeline/` (wraps `services.memories.aggregator.get_memory_events`,
the same `MemoryEvent` data the internal page's map/timeline renders, standard page envelope,
defaults to the trailing 90 days like the internal page); `GET /memories/on-this-day/` (mirrors
`MemoriesOnThisDayView`'s past-year/this-month-day query across visits/routes/photos, kept at its
internal cap of 10 rows per category rather than paginated). `GET /suggestions/pins/` and
`POST /suggestions/pins/{id}/{accept|reject}/` — this is genuinely new: the existing
`POST /pin-suggestions/` route only ever *stages* a suggestion from an external "discovery" app; it
never listed or resolved the `PinSuggestion` review queue that Immich/local-folder batch scans
populate (distinct from the already-shipped `GET /suggestions/visits/`, which is `VisitSuggestion` —
EXIF-derived, for logging a visit at an *existing* pin). Mirrors `VisitSuggestionsView`/
`VisitSuggestionActionView` exactly: not paginated, `photos:read`/`photos:write` (the same
containment-is-structural reasoning as D4 — a batch-scan suggestion is a photo-domain artifact even
though accepting one can create a `Pin`), 404 (not 403) for another profile's suggestion or one
already handled. **Scoped down deliberately**: accept applies only the suggestion's own defaults
(its `suggested_name` for a brand-new pin) — the web review queue's richer accept dialog (manual
name override, label picker, candidate Immich/local-scan photo picker) is not mirrored in this
pass. Confirmed during implementation that this scoping-down needs no Immich-account gating on the
server side: `accept_pin_suggestion` only talks to Immich when the caller passes `asset_ids`
(selected candidate photos), which this endpoint never does, so that whole code path is simply
never reached rather than needing a permission check.

**Messaging** — `MessageSendSerializer.image_uuids` (list of `Image.uuid`), accepted alongside the
existing `image_ids` (integer pks) on `POST /messages/{peer_slug}/` — additive, not a breaking
change: `image_ids` keeps working unchanged, a request may combine both, and `image_uuids` is
documented as the preferred field for new clients. Resolved through a new
`services.direct_messages.resolve_attachment_ids` helper that merges/dedupes both fields into the
one pk list `create_direct_message` already accepted; ownership and not-yet-attached eligibility
are still enforced there exactly as before, regardless of which field an id came from. Group sends
refuse `image_uuids` with 400, same as `image_ids` already did (attachments aren't supported on
group messages yet — see `GroupMessageSendSerializer.unsupported_fields`).

**Games** — five new SpotGuessr endpoints, each a thin wrapper mirroring its internal
`controllers.spotguessr` equivalent exactly rather than re-deriving any game logic:
`POST .../rounds/{round_id}/expire/` (wraps `expire_round_timer`, same server-side-authoritative
timer check as `SpotGuessrRoundTimeoutView`, idempotent no-op response shape);
`POST .../rounds/{round_id}/feedback/` (wraps `relevance.record_feedback`, same
guessed-on-this-round/`EXPLICIT_KINDS` validation as `SpotGuessrPhotoFeedbackView`);
`PATCH /games/spotguessr/preferences/` (writes only `show_ratings_to_friends`, matching
`SpotGuessrSettingsView` — `last_config` stays auto-managed, not exposed as writable here);
`GET /games/spotguessr/eligible-count/` and `GET /games/spotguessr/eligible-pins/` (the latter
paginated, unlike its internal `SpotGuessrPinsView` counterpart, which returns one unbounded list).

**Trivia and Consensus are out of scope for every item in this pass, not silently skipped.**
`urls_games.py`'s own module docstring already states its routes are "SpotGuessr only, and
solo-play only" as a prior deliberate decision — confirmed by grep that zero Trivia/Consensus
routes or views exist anywhere under `external_api/` (only docstring mentions of the words
"trivia"/"consensus", no actual code). The plan's premise that all three games had comparable base
session infrastructure to extend was wrong: there is no Trivia/Consensus external API surface at
all to attach round-timeout/feedback/preferences/eligible-count endpoints to. Building that base
surface would be new-domain API design (session start/list/detail/round/guess, its own scopes,
its own answer-leak whitelist review per D8) — a much larger undertaking than "polish", and not
something this pass attempts. Recorded here as a discovered gap for a future pass, not folded
into "declined".
