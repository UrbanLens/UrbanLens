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

### D4 — §8 Live location → **design settled, build deferred (P2)**

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

Not built this pass (P2). The rest of the partner surface — invites, accept/decline, the shared
chat, and partner-side mark-safe — did ship.

### D5 — §9 `display_name` → **first/last name yes, `username` no**

There is no `display_name` field. `Profile.username`/`first_name`/`last_name` are read-through
properties onto `User`, and every `display_name` on the wire resolves to `username` through
`resolve_visible_identity`.

**Recommendation: expose `first_name`, `last_name` and the six contact methods** — no
uniqueness constraints, no side effects. P2, deferred, not built this pass.

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

(The generic panel listing/fetch endpoints themselves are P2 and deferred. The `api_payload`
interface they will build on shipped, as did the two panel *security* fixes — see Part 4.)

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
- Pagination is unchanged this pass: page-number style almost everywhere, with the pin/tombstone
  sync feeds cursor-based and a few small envelopes non-paginated. The two normalizations
  previously considered here — moving the memories journal onto the standard envelope, and
  wrapping the safety-maps endpoint's bare top-level array — are P2 and deferred. **The bare
  array is worth fixing before v1 is depended on**: it is the one response shape that can never
  gain a field later without breaking clients.
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

Pins: bulk operations, visit `PATCH`, city/state/country as separate fields, pin-note threading
(declined outright, see D1). Lists/labels: REST reorder, bulk delete/edit/convert, list markup
maps. Wikis: ownership and sale history, public-vote ballots, boundary set/clear, detail-pins
CRUD, cover-photo write, history hard-delete, alias nickname toggle. Photos: custom fields,
memories timeline and on-this-day, new-pin suggestions feed. Messaging: photo-uuid attachments
(`image_ids` still wants integer pks), per-conversation retention (blocked on product, see D3).
Safety: live location (design settled, see D4). Social: `first_name`/`last_name` and contact
methods, social links. Misc: enrichment panels as JSON, generic panel listing/fetch, backup
export/import, undo, site config, AI assistant, Immich/Flickr/Google Photos connections,
`last_used_at` on introspection, pagination normalizations. Games: round timeout, photo feedback,
preferences, own-pins feed, area pin count. Also deferred: the styled OAuth consent screen
(currently django-oauth-toolkit's unstyled default, which is the only user-visible gate before an
app is granted `messages:*` against an E2EE mailbox — worth doing before public launch).
