# External API Reference

The complete surface UrbanLens exposes to third-party and first-party (mobile/desktop) clients,
mounted at `dashboard/api/external/v1/` (app namespace `external_api`), plus the E2EE endpoints at
`dashboard/e2ee/` which share the same authentication and are part of the same public contract.

This is a reference, not a tutorial or a changelog — for the reasoning behind recent decisions, see
`docs/notes/mobile_app_notes.md`. For known gaps, see the "Not Yet Implemented" section below and
`docs/PROBLEMS.md`.

Machine-readable schema: `GET schema/` (OpenAPI, no auth required) and a browsable UI at `GET docs/`.
Both are generated from the same `@extend_schema` decorators the endpoints below were checked
against, via `external_api.schema.preprocess_external_api_only`, which filters the published schema
to paths starting `/dashboard/api/external/v1/` plus the anchored `/dashboard/e2ee/` prefix.

## Contents

1. [Authentication](#authentication)
2. [Scopes](#scopes)
3. [Rate limits](#rate-limits)
4. [Conventions](#conventions)
5. [Account & Identity](#account--identity)
6. [Pins](#pins)
7. [Pin Sub-resources](#pin-sub-resources)
8. [Panels](#panels)
9. [Custom Fields](#custom-fields)
10. [Lists & Saved Filters](#lists--saved-filters)
11. [Labels](#labels)
12. [Photos](#photos)
13. [Locations & Suggestions](#locations--suggestions)
14. [Memories](#memories)
15. [Community Wikis](#community-wikis)
16. [Trips](#trips)
17. [Direct Messages & Group Chats](#direct-messages--group-chats)
18. [WebSockets](#websockets)
19. [Safety](#safety)
20. [Device Scanning](#device-scanning)
21. [Friends & Social](#friends--social)
22. [Notifications & Push](#notifications--push)
23. [Games](#games)
24. [Undo History](#undo-history)
25. [Search](#search)
26. [AI Assistant](#ai-assistant)
27. [End-to-End Encryption (E2EE)](#end-to-end-encryption-e2ee)
28. [Not Yet Implemented](#not-yet-implemented)

---

## Authentication

Two credential kinds are accepted, both as `Authorization: Bearer <token>`:

| Kind | Format | Lifetime | Where it comes from |
|---|---|---|---|
| Personal access token (PAT) | `ulk_…` | Long-lived, until revoked | User-generated in account settings |
| OAuth2 access token | Opaque, django-oauth-toolkit | Short-lived (~1 hour) + refresh | Authorization-code + PKCE flow |

The first-party mobile/desktop app is a registered **public** OAuth2 client (`client_id:
urbanlens-mobile`), provisioned by migration (`provision_mobile_oauth_client`) — no client secret,
PKCE required, redirect URIs `urbanlens://oauth/callback` and `http://127.0.0.1/callback` (desktop
loopback, any port). A third-party integration would register its own client the same way.

The consent screen at `/oauth/authorize/` (`oauth2_provider/authorize.html`, overridden under
`dashboard/templates/`) now uses the site's own themed auth shell rather than django-oauth-toolkit's
unstyled Bootstrap-2 default — this is the one user-visible gate before a client is granted a scope
like `messages:*` against an E2EE mailbox, so it matters that it reads as UrbanLens rather than a
generic framework page. Only `authorize.html` was restyled; the token/application management pages
under `oauth2_provider:` (e.g. `authorized-token-list`, used to revoke a connected app) are linked
from it but still render with the toolkit's own default template.

A handful of endpoints (`whoami/`, `settings/`, and the E2EE views under `dashboard/e2ee/`) also
accept the caller's browser session cookie — they extend `DualAuthJsonView` and try session auth
before bearer-token auth, so the site's own logged-in pages can call them directly.

`schema/` and `docs/` are served with no authentication at all — the schema is the published
contract, not user data.

## Scopes

Every credential — PAT or OAuth2 — carries a set of scopes from one shared vocabulary
(`ApiKeyScope`, `dashboard/models/account/model.py`), named `domain:action` so a domain can grow
new endpoints without forcing re-consent. An endpoint declares `required_scopes` per HTTP method;
`HasApiKeyScope` requires the credential to hold **every** listed scope and fails closed — no
declared scopes means access denied, never "no scope needed."

| Scope | Grants |
|---|---|
| `profile:read` | Read your profile UUID |
| `settings:read` / `settings:write` | Read / change your account preferences |
| `pins:read` / `pins:write` | Read (incl. deletions, for sync) / create, edit, delete your pins |
| `lists:read` / `lists:write` | Read / modify your pin lists and saved filters |
| `labels:read` / `labels:write` | Read / create, modify, merge your labels |
| `visits:read` / `visits:write` | Read / log your visit history |
| `photos:read` / `photos:write` | Read photos, memories, suggestions / upload, label, vote, delete |
| `media:read` | Fetch the actual image/video/document files referenced elsewhere |
| `wiki:read` / `wiki:write` | Read / edit community wikis on your behalf |
| `trips:read` / `trips:write` | Read / create and edit your trips |
| `social:read` / `social:write` | Read your friends list / manage friend relationships |
| `safety:read` / `safety:write` | Read / start, update, clear safety check-ins |
| `device_scans:read` / `device_scans:write` | Read nearby *cumulative* device markers / upload wireless device-scan data |
| `messages:read` / `messages:write` | Read / send encrypted messages, manage encryption keys — **OAuth2 only, see below** |
| `notifications:read` / `notifications:write` | Read / mark-read and change delivery prefs |
| `search:read` | Search your pins, wikis, and photos |
| `games:read` / `games:write` | Read game history/scores / play games on your behalf |
| `push:manage` | Register/remove this device's push notifications |
| `custom_fields:read` / `custom_fields:write` | Read your custom field definitions and values / create, edit, delete them |
| `undo:read` / `undo:write` | Read your recent delete history available to undo / restore a previously deleted item |
| `panels:read` | Read pin-detail enrichment panels (boundaries and other plugin-contributed data) |
| `assistant:write` | Chat with your AI assistant, including creating trips and trip activities it suggests |

**`messages:read`/`messages:write` can never be granted to a PAT**, even one hand-edited to carry
them — `external_api.permissions.OAUTH2_ONLY_SCOPES` refuses them for any credential without
`allow_scopes()` (the OAuth2 `AccessToken` method). Direct messages are end-to-end encrypted and
need per-device key material a server-side PAT model doesn't have; OAuth2 tokens are also
short-lived and follow an explicit consent screen naming the capability, where a PAT is a bearer
secret that tends to end up in CI configs and screenshots.

**A freshly issued PAT (`ulk_…`) is granted exactly four scopes and nothing else** —
`profile:read`, `pins:read`, `pins:write`, `push:manage` — with no scope-picker UI yet. Every other
scope in the table above is reachable **only through the OAuth2 + PKCE flow**, where the consent
screen lets the client request precisely what it needs from the full vocabulary. Build against
OAuth2, not a pasted personal key, for anything beyond basic pin sync.

## Rate limits

Three throttles apply to (almost) every request together, keyed per-credential (not per-user or
per-IP, so one misbehaving integration's key doesn't burn another key's budget):

- **Read** (`external_api_read`) — generous hourly cap, sized for a full mobile sync.
- **Write** (`external_api_write`) — tighter hourly cap. A request's tier follows whether any of
  its required scopes ends in `:write`/`:manage`; a view that declares no scopes at all is treated
  as a write (fail closed).
- **Burst** (`external_api_burst`) — a short-window cap on every request regardless of tier.

Several endpoints layer an *additional*, narrower throttle on top of the standard three because
their cost isn't proportional to "one request": location-search/autocomplete (`external_api_location_search`,
charged per keystroke), global search (`external_api_global_search`, fans out across every scoped
domain), smart-list resync and pin wiki-sync (`external_api_resync`, re-evaluates every pin against
a filter+boundary or a geo-containment test), starting a game session (`external_api_game_start`,
40/hour — up to 25 eligibility passes plus a possible billed imagery call), and trip calendar
export/unexport (`external_api_calendar` — talks to Google on the request path, consuming a shared
upstream quota). Media file fetches (`controllers.media.MediaGateView`, `external_api_media`) have
their own separate budget entirely, since a photo gallery screen is legitimately dozens of requests
in a couple of seconds.

## Conventions

- **Error envelope**, uniform everywhere: `{"error": "..."}`, or
  `{"error": "Invalid request.", "fields": {"name": ["..."]}}` for field-level validation failures.
  No endpoint returns a bare `{"detail": ...}` or an unkeyed validation dict.
- **404, not 403**, for anything whose existence the caller hasn't already been shown — a 403 would
  confirm the object exists. The rare genuine exceptions (a non-organizer reading trip settings, a
  non-creator managing trip members, deleting someone else's group message once already a member)
  are cases where the caller was already shown the object, so nothing is leaked; each is commented
  in code as deliberate.
- **Pagination**: page-number style almost everywhere (`{count,next,previous,results}`). The
  pin/tombstone sync feeds and message-thread endpoints are cursor-based instead. A few small
  envelopes remain non-paginated by design (a trip's map markers, the undo feed, nearby device
  markers). `memories/journal/`
  and `safety/checkins/{slug}/maps/` used to be among them (a bare top-level array, and a bespoke
  `{entries,total,omitted_sources}` shape) but were normalized onto the standard envelope before v1
  gained any real client depending on the old shape — see their entries below.
- **Versioning**: `v1` changes additively only. A breaking change mints `/v2/` and serves a
  `Sunset` header on `v1`.
- **WebSockets enforce scopes** exactly like HTTP: `ws/messages/` needs `messages:read` to connect
  and `messages:write` to send, `ws/notifications/` needs `notifications:read`, the safety
  check-in chat needs `safety:*`, and the game sockets need `games:*`. Since `messages:*` is
  OAuth2-only, a PAT can never open `ws/messages/`. Revoking a credential terminates its live
  sockets immediately (checked every 60s on the safety/notification sockets).
- **Idempotency**: several write endpoints accept a client-generated key (`uuid` on pin create,
  `client_uuid` on message send) so a retried request after a lost response replays the original
  result (200/idempotent) instead of duplicating it (201/created).
- **Routing/ordering**: routes are declared across per-domain `urls_*.py` modules and re-sorted by
  path specificity at import time (literal segments always win over a generic `<str:...>`
  converter, regardless of declaration order) — see `external_api/urls.py`'s module docstring if
  you're adding a route and wondering why order in the file doesn't matter.

---

## Account & Identity

`GET /whoami/` — `WhoAmIView` — scopes: `profile:read` — narrowest identity read — response: `{uuid, slug}` (slug backfilled via `Profile.ensure_slug()` — every other endpoint that names people/paths uses slug, so this is the one place a client learns its own).

`GET /auth/session/` — `AuthSessionView` — scopes: session-or-credential (`IsAuthenticated` only, no scope — a client asks this to discover its own grant) — describes the calling credential — response: `{credential_type: "oauth2"|"api_key", scopes[], expires_at, issued_at, client_id, name, user_uuid}` — throttle tier forced to `read` (would otherwise default to `write` since it declares no scopes).

`GET /settings/` — `AccountSettingsView` — scopes: `settings:read` — full account-preferences document (hand-enumerated allowlist, not model-derived — deliberately fails closed on new `Profile` fields) — response: `first_name`/`last_name`(read through to `User`), the six contact methods (`phone_number,signal_username,discord_username,whatsapp_number,telegram_username,matrix_handle`), plus ~50 further fields grouped as privacy visibilities, DM prefs, style/theme, map display/center, markup defaults, places/AI/keyword-tagging feature toggles, history tracking, community/wiki-sync toggles, external-APIs toggle, storage downscaling limits, plus read-only `updated`, `effective_distance_units`, `features{ai,places}`, `allowed_image_dimensions[]`, `allowed_video_heights[]`.

`PATCH /settings/` — `AccountSettingsView` — scopes: `settings:write` — partial update, absent=untouched — request: same field set, all optional — response: full post-save document (never echoes submission — `community_enabled:false` coerces gated visibility/wiki-sync fields off server-side) — 400 `{error, fields: {...}}` per-field (feature-gated fields rejected while the feature is off) — `first_name`/`last_name` save straight to `User`, not `Profile` (no uniqueness check, unlike `username`/`email` which stay off this surface entirely); `discord_username` is charset-checked (`2-100 chars: letters, digits, underscores, dots, hyphens, or #`) — this is the private contact-info Discord field, a separate row from the public one under [Profile Social Extras](#profile-social-extras).

---

## Pins

`GET /pins/` — `PinsView` — scopes: `pins:read` — delta-sync feed, **not a browse endpoint**, ordered `(updated, pk)` — query: `modified_since`(datetime), `cursor`, `limit`(≤1000), `include_total`(bool) — response: `{pins: SyncPin[], next_cursor, sync_watermark, total}`; `SyncPin`: id, uuid, slug, name, icon, description, priority, `last_visited`(ISO or literal `"never"`), latitude, longitude, status, categories[], profile, rating, color, tags[{id,name,color,icon,kind}], address, own_icon, own_custom_icon_url, own_color, child_count, pin_type, parent_uuid, created, updated — hand the returned `sync_watermark` back as next `modified_since`.

`POST /pins/` — `PinsView` — scopes: `pins:write` — creates via the same pipeline as the map's "Add pin" form — request: name, latitude+longitude or address, icon, color, description(≤10000), pin_type, `uuid`(client-generated idempotency key — replay returns the existing pin), parent_id(make a child/detail pin), name_is_user_provided(default false) — response: `{uuid, slug, name, ambiguous_location, created, parent_uuid}`, 201 new / 200 idempotent replay — 403 `PinCreationForbiddenError`; 400 on geocode failure.

`GET /pins/deleted/` — `PinTombstonesView` — scopes: `pins:read` — delta feed of hard-deletions, companion to `pins/` — query: `deleted_since`, `cursor`, `limit` — response: `{tombstones: [{pin_uuid, deleted_at}], next_cursor, sync_watermark}` — **410 Gone** + `{full_resync_required: true}` when `deleted_since` predates the tombstone retention window — client must fully re-walk `pins/` with no `modified_since` and drop locally-held pins absent from the result. (This literal route sits textually behind `pins/<slug>/`; the specificity re-sort guarantees it can never be shadowed.)

`GET /pins/{pin_slug}/` — `PinDetailView` — scopes: `pins:read` — full detail, superset of the sync payload — adds: official_name, `city`/`state`/`county`/`country`/`zipcode`(read-only, geocoded from the pin's Location — never exposed via a direct Location lookup, only through a pin the caller already owns), date_built/date_abandoned/date_last_active, `security{fences,alarms,cameras,security,signs,vps,plywood,locked}`, location_slug(navigate wikis with this, not wiki_slug), wiki_slug(informational only), cover_photo_url, boundary(GeoJSON, nullable), notes[]/aliases[]/links[]/custom_fields[{id,name,type,value}], note_count/alias_count/link_count.

`PATCH /pins/{pin_slug}/` — `PinDetailView` — scopes: `pins:write` — partial; **absent = untouched, explicit null = cleared** — request: name, icon, description, color, pin_type, priority/danger/vulnerability(0-5; publishes/withdraws a community `WikiStatVote` when the matching sync-to-wiki setting is on), last_visited, date_built/date_abandoned/date_last_active, `security{...}`(partial, 8 fields, `unknown` clears), `label_uuids[]`(**full replacement**, not delta, of tag/category/status labels only — removed ones tombstoned so auto-tagging can't re-add them), `visited`(bool convenience toggle — mutually exclusive with an explicit `last_visited` in the same call), latitude+longitude(must both be present; relinks Location), parent_id(uuid to reparent, null to detach), confirm_wiki_loss — response: full detail — `rating` NOT accepted here (use `/review/`); address/city/state/country/official_name read-only (derived from Location); **409** `{requires_wiki_loss_confirmation: true, wikis:[{name,slug}]}` when a move would end community-wiki access — resend with `confirm_wiki_loss:true`.

`DELETE /pins/{pin_slug}/` — `PinDetailView` — scopes: `pins:write` — stages an Undo History entry + tombstone — query: `children=delete|keep` — **409** `{requires_children_decision: true, children: <count>}` if the pin has children and the param is omitted.

`POST /pin-suggestions/` — `PinSuggestionsView` — scopes: `pins:write` — stages a **pending `PinSuggestion`**, not a real pin (owner must accept/reject from Memories → Locations); merges into an existing pin/pending suggestion via the same clustering pipeline as Immich/local-scan hits — request: name, latitude+longitude or address, description, pin_type, `aliases[]`(≤10), `links[{name,url}]`(≤10, http/https only), `photos[]`(URLs to download, ≤3) — response 201: `{suggestion_id, status, matched_existing_pin, photos_attached, review_url}` — 403 if visit-history tracking is off (a suggestion is itself a location-history trail); 403 if address-only and external lookups are disabled.

**Bulk operations** — thin wrappers over the main map's multi-select toolbar (`dashboard/controllers/pin_bulk.py`), routed from `urls_pin_extra.py`. Every pin named that isn't the caller's own is silently ignored rather than refused (an offline client replaying a queued batch shouldn't fail wholesale over one pin gone meanwhile), but an unresolvable `add_label_uuids`/`remove_label_uuids`/`parent_uuid` value is a 400 — the caller asked for something specific and impossible, not something merely stale.
- `POST /pins/bulk/delete/` — `PinBulkDeleteView` — `pins:write` — request: `{uuids[]}`(≤500) — deletes each pin's full detail-pin subtree, stashing one `UndoAction` covering everything removed — response: `{deleted, descendant_count, total_count, undo_uuid}` — restore via the existing generic `POST /undo/{undo_uuid}/restore/` (needs `undo:write` too), not a bulk-specific undo endpoint — 404 if none of `uuids` are the caller's.
- `POST /pins/bulk/merge/` — `PinBulkMergeView` — `pins:write` — request: `{target_uuid, source_uuids[]}`(≤500) — every source becomes a detail pin of the target; a target that's currently itself a detail pin is promoted to top-level first — response: `{target: PinSummary, merged_uuids[], skipped_uuids[]}` (skips are sources that would have made the target their own descendant) — 400 if promoting the target would collide with another top-level pin at the same location, or if nothing ended up merged — 404 if `target_uuid` isn't the caller's.
- `POST /pins/bulk/edit/` — `PinBulkEditView` — `pins:write` — request: `{uuids[]}`(≤500) plus any of `description`(absent=untouched, null/empty=cleared), `rating`(1-5 sets a `Review` for each pin, null clears it — absent=untouched; kept off single-pin PATCH for the same reason, see `/review/` above, but bulk has no per-pin alternative), `add_label_uuids[]`/`remove_label_uuids[]`(delta, not a replacement — contrast single-pin PATCH's `label_uuids`; removals write the same `PinAutoRemoval` tombstone so auto-tagging can't put a label back), `parent_uuid`(reparent every pin under one target, null detaches to top-level) — response: `{count, reparented}` — 404 if none of `uuids` are the caller's.

---

## Pin Sub-resources

**Notes** (private, append-only — no update; delete+recreate to edit)
- `GET/POST /pins/{pin_slug}/notes/` — `PinNotesView` — `pins:read`/`pins:write` — paginated (25/page, ≤100). POST: `text`(≤50000). Response: `{id, text, created, updated}`.
- `DELETE /pins/{pin_slug}/notes/{note_id}/` — `PinNoteDetailView` — `pins:write`.

**Aliases** (full name history, incl. current name — flagged, not omitted)
- `GET/POST /pins/{pin_slug}/aliases/` — `PinAliasesView` — `pins:read`/`pins:write`. POST: `name`, `kind`(default `ALTERNATE`). Response: `{id, name, kind, source(read-only), created, is_current}` — 409 duplicate name (case-insensitive).
- `DELETE /pins/{pin_slug}/aliases/{alias_id}/` — `pins:write` — 400 if deleting the current name; writes a tombstone.
- `POST /pins/{pin_slug}/aliases/{alias_id}/use/` — `PinAliasUseView` — `pins:write` — promotes alias to current name; response is the full pin detail (not the alias).

**Links**
- `GET/POST /pins/{pin_slug}/links/` — `PinLinksView` — `pins:read`/`pins:write` — ordered `order,pk`. POST: name(optional), url(http/https, ≤2000). Response: `{id, name(=display_name), url, wayback_url(nullable), order, created}` — 400 non-http(s) url.
- `DELETE /pins/{pin_slug}/links/{link_id}/` — `pins:write`.

**Visits** — own scope family (`visits:*`, not `pins:*`) so a client can be granted a pin's contents without the owner's movement history
- `GET/POST /pins/{pin_slug}/visits/` — `PinVisitsView` — `visits:read`/`visits:write` — most-recent-first. POST: `visited_at`(required), `notes`(≤50000). Response: `{id, uuid, visited_at, notes, source(read-only), tentative(read-only), photo_count, created, updated}` — 403 if visit-tracking off.
- `PATCH /pins/{pin_slug}/visits/{visit_id}/` — `PinVisitDetailView` — `visits:write` — partial, absent=untouched — request: `visited_at`, `notes` (the same two fields POST accepts — participants/photos/markup-map stay the web dialog's concern) — re-derives the pin's last-visited date on any change — response: full visit.
- `DELETE /pins/{pin_slug}/visits/{visit_id}/` — `visits:write` — re-derives the pin's last-visited date.

**Comments** — owner's private annotation, scoped `pins:*` (not `wiki:*`)
- `GET/POST /pins/{pin_slug}/comments/` — `PinCommentsView` — `pins:read`/`pins:write`. POST: text(≤1000), parent_id(reply, optional). Response: `{id, text, mentions[{display,location_slug}], author(masked), author_is_self, image_url, has_map, reactions:{emoji:{count,reacted}}, parent_was_deleted, created, replies(one level deep)}`.
- `DELETE /pins/{pin_slug}/comments/{comment_id}/` — `pins:write` — scoped to caller's own comment on their own pin.
- `PUT/DELETE /pins/{pin_slug}/comments/{comment_id}/reactions/{emoji}/` — `pins:write` — declarative set/unset (not toggle). Response: `{reactions: {emoji: {count, reacted}}}`.

**Review** (the pin's `rating` — deliberately excluded from pin PATCH; the only write path for it)
- `GET/PUT/DELETE /pins/{pin_slug}/review/` — `PinReviewView` — `pins:read`/`pins:write`/`pins:write` — body/response `{rating}`(0-5) — GET 404 if unrated; PUT 200 or 201; DELETE 204/404.

**Article** (pin's own private long-form write-up, same Article/ArticleRevision machinery as a community wiki, deliberately scoped `pins:*` not `wiki:*`)
- `GET/PUT /pins/{pin_slug}/article/` — `PinArticleView` — `pins:read`/`pins:write`. GET: `{id, content(markdown), content_html(sanitized), toc, word_count, last_edited_by, updated, base_revision_id}` — 404 if never written. PUT: `content`(≤200000), `edit_summary`(≤255), `base_revision_id`(**required, nullable** — creates article on first save) — **409** `{error, conflict:true, current_revision_id}` on concurrent edit, nothing written.
- `GET /pins/{pin_slug}/article/revisions/` — `pins:read` — paginated, newest first.
- `GET /pins/{pin_slug}/article/revisions/{revision_id}/` — `pins:read` — adds `content` + `diff:[{kind,text}]` vs predecessor.
- `POST /pins/{pin_slug}/article/revisions/{revision_id}/restore/` — `pins:write` — appends old content as a new revision tagged `restored_from`.

**Wiki-sync** (manual pin↔wiki child-marker sync; both stack `ExternalApiResyncThrottle` on top of the standard three)
- `POST /pins/{pin_slug}/wiki-sync/push/` — scopes: **`pins:write` AND `wiki:write`** — publishes selected child pins as child wikis. Request: `{child_pin_uuids: [uuid,...]}`(1-500). Response: `{created, wiki_exists}` — **200 with `wiki_exists:false`, not 404**, when the property has no community wiki yet.
- `POST /pins/{pin_slug}/wiki-sync/pull/` — scopes: **`pins:write` AND `wiki:read`** — creates personal child pins for wiki markers not already covered. Same response shape.

**Pin shares**
- `POST /pin-shares/{share_id}/respond/` — `PinShareRespondView` — scopes: `pins:write`(not `messages:*` — shares also arrive as bare notifications) — request: `{action: "accept"|"reject"}` — response: `{status, pin_slug(null on reject), detail}` — 404 if share doesn't exist *or* addressed to someone else; 400 if already handled; deliberately does **not** call `record_share_exposure`.

---

## Panels

Pin-detail enrichment panels (the same `PanelSource` plugin data backing the internal HTMX tab strip) as JSON — a job-shaped ask/cached-or-pending/poll surface, since a cold fetch talks to an upstream provider from a Celery worker, never on the request thread.

`GET /pins/{pin_slug}/panels/` — `PinPanelsListView` — scopes: `panels:read` — every panel exposed to this API for this pin, with its readiness. A source is listed only when it has declared a non-empty `api_kinds` (the panel author's explicit opt-in — see below), passes its own `gate(pin)` precondition (e.g. has usable coordinates), and the caller holds whatever `SiteFeature` the source requires, if any — a feature-gated source the caller can't see is omitted entirely, the same rule the web tab strip applies. Response: `[{key, kinds[], ready}]`.

`GET /pins/{pin_slug}/panels/{key}/` — `PinPanelDetailView` — scopes: `panels:read` — an unknown key, empty `api_kinds`, failed `gate`, or an ungranted feature gate all answer **404**, identically — never 403, which would confirm a paywalled panel has something to say about this specific pin. Ready → **200** with the panel's own `api_payload(pin)` body (shape varies per `PanelApiKind` — `info`/`media`/`boundary`/`buildings` — declared per source, not fixed by this endpoint). Ready but genuinely empty → **204** (a media search that found zero results is a real answer, not a missing one). Not ready → schedules a fetch and answers **202** `{"ready": false, "poll_after_seconds": N}` — `N` is 2 seconds while a fetch is in flight, larger if scheduling failed (broker down, or this source was recently suppressed after a failure).

**`satellite` and `street_view` are permanently excluded** — empty `api_kinds` by design, not an oversight (see D8 in `docs/notes/mobile_app_notes.md`): their web payload is base64 `data:` URIs (5-15MB/response), and this API's throttle counts requests, not bytes, so exposing them would hand any key holder an unmetered bandwidth amplifier. Needs a signed slide-image proxy that doesn't exist yet.

**A panel source is closed to the API by default.** `PanelSource.api_kinds` defaults to an empty frozenset at the base class — the authoritative "not on the API" signal, independent of whether `api_payload()` happens to return data right now (it also returns `None` whenever the data simply hasn't landed yet). `InfoPanelSource`/`GalleryMediaSource` (the two most common plugin base classes) default `api_kinds` to non-empty, so a plugin author gets API exposure automatically unless they opt back out — five built-in plugins do so deliberately: `property_records`, `loopnet`, `yelp`, `google_places` (photos), and `google_images`, each citing a third-party redistribution/ToS restriction or a photo path that only resolves through an internal session-authenticated proxy anyway. EPA ECHO's nearby-facilities panel stays exposed *and* feature-gated (`NEARBY_RESEARCH`); its exact-site compliance card stays exposed and ungated, since that half is public government data by design.

---

## Custom Fields

Field *definitions* (shared across every entity type: pins, photos, profiles, markup maps) plus read/write of PHOTO-entity *values*. A pin's own custom-field values remain readable, read-only, embedded on `GET /pins/{pin_slug}/` (`custom_fields[{id,name,type,value}]`) — writing a pin's values through this domain is not yet exposed.

`GET /custom-fields/` — `CustomFieldDefinitionsView` — scopes: `custom_fields:read` — the caller's own field definitions, paginated — query: `entity_type`(`pin`|`photo`|`profile`|`markup_map`) — response rows: `{id, entity_type, name, field_type, options[](select only), order}`.

`POST /custom-fields/` — scopes: `custom_fields:write` — request: `entity_type`, `name`, `field_type`(`text`|`number`|`date`|`time`|`select`|`checkbox`|`url`|`reference` — **`reference` is accepted for parity but not yet writable as a value**, since resolving it needs its own target-lookup design), `options[]`(required, deduplicated, for `select`), `order` — 400 on a duplicate `(entity_type, name)` pair or an empty `options[]` on a `select` field.

`PATCH /custom-fields/{field_id}/` — scopes: `custom_fields:write` — partial update — 400 if changing `field_type` while values already exist for this field (would silently corrupt stored values), or removing a `select` option a stored value still uses — `entity_type` is ignored if sent (immutable after creation).
`DELETE /custom-fields/{field_id}/` — scopes: `custom_fields:write` — cascades to every stored value.

`GET /photos/{image_uuid}/custom-fields/` — `PhotoCustomFieldsView` — scopes: **`custom_fields:read` AND `photos:read`** — every field defined for the `photo` entity type, `value: null` when unset on this photo (not omitted) — response: `[{id, name, type, value}]`.
`PUT /photos/{image_uuid}/custom-fields/{field_id}/` — scopes: **`custom_fields:write` AND `photos:write`** — request: `{value: str}` — typed parsing/validation per the field's own `field_type` (400 on a value that doesn't parse) — an empty value **clears** the stored value (**204**) rather than storing a blank.
`DELETE /photos/{image_uuid}/custom-fields/{field_id}/` — same scopes — clears the value, same as an empty PUT.

---

## Lists & Saved Filters

`GET /lists/` — `PinListsView` — scopes: `lists:read` — paginated. Query: `is_smart`(bool) — response: `{uuid, slug, name, description, is_smart, pin_count, smart_filter(JSON criteria), has_boundary(bool — geometry withheld from list view), source_saved_filter_uuid, created, updated}`.

`POST /lists/` — `lists:write` — request: name, description(≤50000), is_smart, smart_filter(JSON, ownership-validated against caller's own labels/custom fields), smart_boundary(GeoJSON, ≤20000 vertices), source_saved_filter_uuid(copies criteria in) — response 201 (includes full boundary geometry) — 400 duplicate name/unowned criteria reference — resyncs membership immediately if rules were given.

`GET /lists/{list_slug}/` — `lists:read` — resolves by slug or uuid.
`PATCH /lists/{list_slug}/` — `lists:write` — resyncs membership **only** when `is_smart`/`smart_filter`/`smart_boundary` actually changed (not on a plain rename).
`DELETE /lists/{list_slug}/` — `lists:write` — 204; member pins untouched.

`GET /lists/{list_slug}/items/` — `lists:read` — paginated, ordered `(order, created, pk)` — response: `{id, order, added_via(manual|smart_filter|boundary), pin:{uuid, slug, name(=effective_name), latitude, longitude}}`.
`POST /lists/{list_slug}/items/` — `lists:write` — request: `{pin_uuids:[...]}`(1-500) — response: `{added, skipped_over_cap, max_pins}` — unknown/foreign uuids silently dropped.
`DELETE /lists/{list_slug}/items/` — `lists:write` — **DELETE carries a body** (batch removal): `{pin_uuids:[...]}` — response: `{removed}`.
`POST /lists/{list_slug}/items/reorder/` — `lists:write` — request: `{item_ids:[...]}`(PinListItem ids, not pin uuids; 1-1000) — stale/foreign ids ignored.
`POST /lists/{list_slug}/resync/` — `lists:write` — no body; response: `{pin_count}` — synchronous full re-evaluation of every owned pin; stacks `ExternalApiResyncThrottle`.

`GET /saved-filters/` — `SavedFiltersView` — `lists:read` — paginated — `{uuid, name, icon, criteria(JSON), order, created, updated}`.
`POST /saved-filters/` — `lists:write` — request: name, icon(default "bookmark"), criteria(JSON, ownership-validated), order — 400 duplicate name per profile.
`GET /saved-filters/{filter_uuid}/` — `lists:read`.
`PATCH /saved-filters/{filter_uuid}/` — `lists:write` — response adds `lists_resynced`(count) — changing `criteria` resyncs every `PinList` derived from this filter (one-time copy, not a live reference).
`DELETE /saved-filters/{filter_uuid}/` — `lists:write` — 204; derived lists keep last-copied criteria (FK is SET_NULL).

---

## Labels

Every write below to a Tag or Category label (never Status, People, or Media) - including
merges, and pin/location assignment changes made through any endpoint that touches
`Pin.labels` - is transparently synced to REData's label-suggestion service in the background
(`services.labels.redata_suggestions`); a REData outage or missing configuration is a silent
no-op, never a failed request.

`GET /labels/` — `LabelsView` — scopes: `labels:read` — paginated. Query: `kind`, `is_global`(bool), `q`(name icontains), `parent_uuid`, `with_counts`(opt-in) — response: `{uuid, name(own), effective_name(customized), description, kind, color/effective_color, icon/effective_icon, custom_icon_url, order, is_protected, allow_auto_tag, keywords, is_global, is_customized, is_editable, parent_uuids[], pin_count/location_count(only with with_counts), created, updated}`.

`POST /labels/` — `labels:write` — request: name, description, kind(required), color, icon, order, allow_auto_tag, keywords, `parent_uuids[]`(≤50) — always created under caller's own profile (never global) — 400 if kind missing or parent uuid not visible.

`GET /labels/{label_uuid}/` — `labels:read` — works for any visible label, including global.
`PATCH /labels/{label_uuid}/` — `labels:write` — `kind` silently ignored on update — **403** if the label is global or `is_protected` (use customization instead) — 400 if a parent assignment would close a hierarchy cycle.
`DELETE /labels/{label_uuid}/` — `labels:write` — same 403 rule; pins carrying it simply lose the reference.

`PUT /labels/{label_uuid}/customization/` — `labels:write` — works on **any** visible label incl. global — per-profile display override, invisible to everyone else. Request: `{name, icon, color}`(all optional/nullable) — submitting all-blank fields deletes the override.
`DELETE /labels/{label_uuid}/customization/` — `labels:write` — clears override; response 200 (not 204) with refreshed label.

`POST /labels/{label_uuid}/merge/` — `LabelMergeView` — `labels:write` — merges `source_uuids[]` into the URL label (the **target**, which survives); sources must be caller's own, unprotected, same kind, never global. Request: `{source_uuids:[...]}`(1-100). Response: `{target, merged_uuids[], pins_moved}` — **destructive, not covered by Undo History, cannot be undone**.

---

## Photos

`GET /photos/` — `PhotosView` — scopes: `photos:read` — browse (paginated), **not a sync feed** — no tombstone endpoint. Query: `pin`(slug/uuid, own pins only), `unfiled`(bool), `taken_from`/`taken_to`, `media_type` — response: `{uuid, media_type, source, url(authenticated media-gate path), caption, author, source_url, copyright, latitude, longitude, coordinates_are_estimated, direction, taken_at, created, file_size, labels[], organize_dismissed, state, owner_slug(masked), pin_slug/pin_name/visit_id(owner-only), wiki_slug/wiki_name(gated), dm_peer_slug/dm_peer_name(only if viewer is a DM participant)}`.

`POST /photos/` — `photos:write` — multipart upload. Request: file, caption(≤500), pin(slug/uuid), visit(PinVisit id) — response 201 — EXIF-derived fields filled asynchronously, typically still null in this response — errors at 400/403/409/413 (malware/size/duplicate/quota/feature-gate).

`GET /photos/{image_uuid}/` — `photos:read` — widens to friends/community-visible photos if not caller's own (the one read endpoint in this domain that does).
`DELETE /photos/{image_uuid}/` — `photos:write` — owner-only, never widened.

`PUT /photos/{image_uuid}/labels/` — `photos:write` — full replacement. Request: `{labels:[...]}`(≤25, each ≤255 chars).

`POST /photos/{image_uuid}/vote/` — `photos:write` — community relevance vote; only meaningful for a photo materialized into a Location's media gallery. Request: `{value: -1|0|1}`(0 withdraws) — response: `{score, your_vote}` — 400 if not gallery-backed. A non-zero vote is also forwarded to REData's photo-relevance model as training signal (`services.photos.redata_relevance`), best-effort.

`POST /photos/{image_uuid}/file/` — `photos:write` — files an unfiled photo onto an existing pin (logs a visit) or creates a new pin from coordinates. Request: `pin`(existing, optional) OR `latitude`/`longitude` + `name` — 409 if already filed; 403 if visit-history tracking off.

---

## Locations & Suggestions

`GET /locations/search/` — `LocationSearchView` — scopes: `pins:read` — merged autocomplete over caller's own pins ("local") and an external places provider ("places") in one call. Query: `q`(min 2 chars), `sources`(csv, unrecognized dropped), `limit`(1-25, default 15) — response: `{results:[{type, title, subtitle, lat, lng(null until resolved via /resolve/), zoom, icon, pin_slug, place_id, is_child}], places_disabled}` — own throttle (`LocationSearchThrottle`, charged per keystroke).

`GET /locations/resolve/` — `PlaceResolveView` — scopes: `pins:read` — resolves an autocomplete `place_id` to coordinates. Query: `place_id` — response: `{lat, lng, name}` — 400 missing place_id; **403** if external lookups are off; 503 no provider configured; 404 unresolvable.

`GET /suggestions/visits/` — `VisitSuggestionsView` — scopes: `photos:read` — caller's pending photo-derived visit suggestions (PENDING only), not paginated — response: `{suggestions:[{id, status, photo, pin_slug, pin_name, suggested_at, visit_date}]}`.

`POST /suggestions/visits/{suggestion_id}/{action}/` — scopes: `photos:write` — `action` is `accept` or `dismiss` (404 for anything else) — 204 — 403 if accepting requires visit-logging and it's off.

`GET /suggestions/pins/` — `PinSuggestionListApiView` — scopes: `photos:read` — caller's pending batch-scan pin suggestions (Immich library sweep, local-folder scan, or an external app's own submission via `POST /pin-suggestions/`), PENDING only, not paginated — response: `{suggestions:[{id, status, origin, is_new_pin, pin_slug, pin_name, latitude, longitude, hit_count, visit_dates, suggested_name, suggested_description, suggested_pin_type, suggested_aliases, suggested_links, created}]}`.

`POST /suggestions/pins/{suggestion_id}/{action}/` — scopes: `photos:write` — `action` is `accept` or `reject` (404 for anything else) — accepts using the suggestion's own defaults (its `suggested_name` for a brand-new pin; no label or candidate-photo selection — the web review queue's richer accept dialog is not mirrored here) — 204 — 404 for another profile's suggestion or one already handled.

---

## Memories

`GET /memories/journal/` — `MemoriesJournalView` — scopes: `photos:read` — unified journal (visit notes, ratings, comments, article edits), newest first. Query: `page`, `page_size` (standard pagination, `page_size` up to 100) — response: the standard `{count, next, previous, results:[{kind, occurred_at, icon, title, subtitle, body, url, rating}]}` envelope plus `omitted_sources` (journal sources — `visits`/`reviews`/`comments`/`articles` — dropped because the credential lacks that source's domain scope; empty for a session caller or a fully scoped credential).

`GET /memories/timeline/` — `MemoriesTimelineView` — scopes: `photos:read` — the same map/timeline data the internal Memories page renders (routes, trips, visits, photos), newest first. Query: `start`, `end` (ISO dates, default to the trailing 90 days), `bbox` (`minLat,minLng,maxLat,maxLng`, malformed values silently ignored), plus standard `page`/`page_size` — response: the standard `{count, next, previous, results:[{type, occurred_at, ended_at, title, subtitle, latitude, longitude, url, thumbnail_url, icon, color, extra}]}` envelope.

`GET /memories/on-this-day/` — `MemoriesOnThisDayApiView` — scopes: `photos:read` — past-year visits/routes/photos matching today's month/day, capped at 10 rows per category (not paginated) — response: `{today, visits:[{pin_slug, pin_name, visited_at, notes}], routes:[{uuid, name, started_at, distance_meters, path}], photos:[...same shape as GET /photos/]}`.

---

## Community Wikis

Every wiki-scoped handler resolves `location, wiki, profile = resolve_visible_wiki(request, location_slug)` first (`services/wiki/wiki_access.py`), which raises a bare `Http404` for all three of: (1) an unknown `location_slug`, (2) a real Location with no Wiki, and (3) a real Wiki the caller hasn't pinned/earned access to. These three are byte-for-byte indistinguishable **on purpose** — a distinguishable response would let a caller use the slug as an oracle for which locations other users have pinned. Every sub-resource id (edit, alias, link, comment, revision) is looked up scoped to the already-resolved wiki, never by bare pk.

### Wikis

`GET /wikis/{location_slug}/` — `WikiDetailApiView.get` — scopes: `wiki:read` — full wiki detail — response: `location_slug, wiki_slug, uuid, name, description, pin_type, indoor_outdoor, date_abandoned, date_last_active, security{8 fields}, latitude, longitude, address, cover_photo_url, boundary, aliases[], links[], stats{danger/vulnerability/priority/rating}, pin_count_low, pin_count_approx, first_pinned(+precision), article{summary}, comment_count, created, updated`.

`PATCH /wikis/{location_slug}/` — scopes: `wiki:write` — apply a community edit — request: `name?, description?, date_abandoned?, date_last_active?, security?{fences,alarms,cameras,security,signs,vps,plywood,locked}` (all optional; unknown top-level keys or empty payload → 400) — strict validation (400 on unrecognized enum/date, unlike the internal form's silent skip); records a `WikiEdit` audit row.

`GET /wikis/{location_slug}/history/` — scopes: `wiki:read` — paginated edit history, newest first — rows: `{id, changes({field:{from,to}}), reverted, editor(masked), created}`.

`POST /wikis/{location_slug}/history/{edit_id}/revert/` — scopes: `wiki:write` — undo one past edit, recorded as a new edit — response: wiki detail + `skipped_fields` — 400 if already reverted; 409 if every touched field was changed again since.

`GET/PUT/DELETE /wikis/{location_slug}/votes/{field}/` — `WikiStatVoteApiView` — scopes: `wiki:read`/`wiki:write`/`wiki:write` — composite score + caller's own vote for one stat field (danger/vulnerability/priority/rating) — response: `{rounded, exact, count(privacy-fuzzed), my_vote}` — PUT idempotent replace; `field` outside the 4 valid values → 404.

### Wiki Article & Revisions

`GET/PUT /wikis/{location_slug}/article/` — scopes: `wiki:read`/`wiki:write` — GET: `{id, content(markdown), content_html, toc, word_count, last_edited_by(masked), updated, base_revision_id}` (404 if none yet). PUT: `content, edit_summary?, base_revision_id`(**required, nullable**) — **409** `{error, conflict:true, current_revision_id}` on concurrent edit, nothing written.

`GET /wikis/{location_slug}/article/revisions/` — scopes: `wiki:read` — paginated, newest first — `{id, edit_summary, editor(masked), size_delta, restored_from, created}`.
`GET /wikis/{location_slug}/article/revisions/{revision_id}/` — scopes: `wiki:read` — adds `content, diff[{kind,text}]` vs predecessor (no line numbers by design).
`DELETE /wikis/{location_slug}/article/revisions/{revision_id}/` — scopes: `wiki:write` — self-service scrub of a revision the caller themselves authored (`ArticleRevision.editor`) — someone else's revision, or one with no editor (system-seeded), → 404. Never touches the article's current text (`Article.content` is its own field, not derived from history); a later restore that pointed at the deleted revision has its `restored_from` set to null automatically.
`POST /wikis/{location_slug}/article/revisions/{revision_id}/restore/` — scopes: `wiki:write` — restore as newest (append-only, tagged `restored_from`).

### Wiki Comments & Reactions

`GET/POST /wikis/{location_slug}/comments/` — scopes: `wiki:read`/`wiki:write` — paginated thread, top-level + one level of replies. Rows: `{id, text(raw markup), mentions[{display,location_slug}], author(masked), author_is_self, image_url, has_map, reactions({emoji:{count,reacted}}), parent_was_deleted, created, replies[]}` — visibility gated incl. an `@location` mention gate that drops (not redacts) comments the viewer hasn't earned access to. POST: `text, parent_id?`.

`DELETE /wikis/{location_slug}/comments/{comment_id}/` — scopes: `wiki:write` — caller's own comment only; someone else's id → 404.

`PUT/DELETE /wikis/{location_slug}/comments/{comment_id}/reactions/{emoji}/` — scopes: `wiki:write` — declarative set/unset (not toggle) of caller's emoji reaction — emoji restricted to an allowlist (👍👎❤️😂😮😢🔥🏚️), checked before target resolution (can't probe id existence) — response: `{reactions: {emoji:{count,reacted}}}`.

### Wiki Aliases & Links

`GET/POST /wikis/{location_slug}/aliases/` — scopes: `wiki:read`/`wiki:write` — rows: `{id, name, kind, source, is_current}`.
`DELETE /wikis/{location_slug}/aliases/{alias_id}/` — scopes: `wiki:write`.
`POST /wikis/{location_slug}/aliases/{alias_id}/use/` — scopes: `wiki:write` — promote to current community name; idempotent; recorded as a wiki edit (revertible via history) — response: full wiki detail.
`POST /wikis/{location_slug}/aliases/{alias_id}/toggle-nickname/` — scopes: `wiki:write` — flip nickname-only ↔ alternate; a display preference on shared data, not a rename, so unlike alias-use it is **not** recorded in the wiki edit history — response: the updated alias.

`GET/POST /wikis/{location_slug}/links/` — scopes: `wiki:read`/`wiki:write` — ordered by `(order, pk)` — rows: `{id, name, url, wayback_url, order}`.
`DELETE /wikis/{location_slug}/links/{link_id}/` — scopes: `wiki:write`.

### Wiki Gallery

`GET /wikis/{location_slug}/gallery/` — scopes: `wiki:read` — paginated shared photo gallery, filtered through uploader visibility + viewer's own photo filter — rows: `{id, url, caption, author, source_url, copyright, created}` — **read-only**; upload deferred (needs the async malware-scan handshake the comment-image path uses). Ordered by REData's cached photo-relevance confidence first, upload recency as the tiebreaker/fallback (`services.photos.redata_relevance`).

### Wiki Boundary, Cover Photo & Property Records

`GET/POST /wikis/{location_slug}/boundary/` — scopes: `wiki:read`/`wiki:write` — the wiki page map's typed (property/building) boundaries — GET response: `{latitude, longitude, default_radius_meters, pending, refreshing, boundaries:{property:{polygon,source}, building:{polygon,source}}}` (`source` one of `wiki`/`generated`/`circle`/`null`; `pending`/`refreshing` drive the same generation-polling contract as the internal map) — POST: `{boundary_type: "property"|"building", polygon: <GeoJSON Polygon|MultiPolygon|null>}`, null clears the community drawing back down the resolution chain — 400 on an oversized or malformed polygon — recorded as a wiki edit.

`PUT/DELETE /wikis/{location_slug}/cover-photo/` — scopes: `wiki:write` — PUT: `{image_uuid}`, must already be in the wiki's own gallery (404 otherwise) — DELETE clears it — response: `{cover_photo_url}`.

`GET /wikis/{location_slug}/ownership/` — scopes: `wiki:read` — paginated shared owner records (`WikiOwner`) currently or previously linked to this place — rows: `{id, name, company_name, address, phone, email, notes, source, created, updated}` — **read-only this pass**; see `docs/notes/mobile_app_notes.md` Part 7 for why the write side (which does exist internally) is deferred.

`GET /wikis/{location_slug}/sales/` — scopes: `wiki:read` — paginated shared sale history (`WikiPropertySale`), newest first — rows: `{id, sale_price, sale_date, notes, source, previous_owners:[{id,name}], new_owners:[{id,name}], created}` — read-only, same reason as Ownership above.

---

## Trips

All trip views map service exceptions uniformly: not-found→404, permission→403, validation→400. Scopes: `trips:read` (GET), `trips:write` (write) — no per-endpoint overrides. **403-vs-404 convention**: a trip the caller isn't a member of is always 404 (never 403) — the lookup can't distinguish "doesn't exist" from "not yours." A real 403 only reaches a caller who is already a member but lacks the specific permission for the action.

### Trips Core

- `GET /trips/` — list the caller's trips — query: `sort`(`start_date`|`updated`, default `updated`), `dir`(`asc`|`desc`, default `desc`) — response: paginated `{uuid, slug, name, description, start_date, end_date, timeline_status, duration_days, activity_count, member_count, comment_count, pin_count, is_creator, membership_status, rsvp, is_organizer, created, updated}`.
- `POST /trips/` — create, or replay an idempotent create via `uuid` — response: **201** genuine create, **200** replayed.
- `GET /trips/{trip_slug}/` — full trip: summary fields + `creator`(masked), `permissions`(4 levels), `viewer{has_joined, is_organizer, is_creator, membership_status, rsvp, can_add_members, can_add_activities, can_edit_activities, can_comment}`, `calendar_sync{connected, linked, auto_sync, last_synced}`, `members[]`.
- `PATCH /trips/{trip_slug}/` — partial metadata update (`name?, description?, start_date?, end_date?`).
- `DELETE /trips/{trip_slug}/` — delete (stages Undo History) — 204.
- `GET /trips/{trip_slug}/map/` — trip's map markers — query: `include_past`(default false) — response: `{"points": [{index, activity_id, label, lat, lng, status, scheduled_at, draggable, child_trip?}]}` — **deliberately unpaginated**.
- `PATCH /trips/{trip_slug}/settings/` — set one or more of 4 permission levels (`allow_add_members?, allow_add_activities?, allow_edit_activities?, allow_comments?`, each `"none"|"organizers"|"everyone"`) — **the domain's explicit 403 case**: a non-organizer member gets 403, not 404.

### Trip Membership & RSVP

- `POST /trips/{trip_slug}/join/` — accept an invitation.
- `DELETE /trips/{trip_slug}/leave/` — leave, or decline an invite — **the creator is refused with 400** (must delete or transfer instead).
- `PUT /trips/{trip_slug}/rsvp/` — set/clear caller's trip-wide RSVP (`yes`|`no`|`maybe`|null).
- `GET /trips/{trip_slug}/members/` — the roster, paginated — `{profile(masked), status(invited|joined), rsvp, is_organizer, is_creator, created}`.
- `POST /trips/{trip_slug}/members/` — invite by username — **201** new invite, **200** re-invite (no duplicate notification).
- `PATCH /trips/{trip_slug}/members/{member_slug}/` — set a member's organizer flag (`is_organizer`, explicit target) — **creator only**, else 403.
- `DELETE /trips/{trip_slug}/members/{member_slug}/` — remove a member — **members may remove themselves; otherwise creator only** → 403.

### Trip Activities

- `GET /trips/{trip_slug}/activities/` — itinerary — query: `include_legs`(default false — **off by default, each leg can trigger a live OSRM routing call**).
- `POST /trips/{trip_slug}/activities/` — add a stop (`title?, notes?, scheduled_at?, scheduled_end?, pin_slug?, location_uuid?, latitude?, longitude?, place_name?, child_trip_uuid?, status?, location_hidden?`) — 403 gated by `allow_add_activities`.
- `PATCH/DELETE /trips/{trip_slug}/activities/{activity_id}/` — 403 gated by `allow_edit_activities`.
- `POST /trips/{trip_slug}/activities/{activity_id}/position/` — persist a map-drag override (`lat`, `lng`, both bounds-checked) — requires `allow_edit_activities`.
- `PUT /trips/{trip_slug}/activities/{activity_id}/vote/` — set/clear caller's vote (`up`|`down`|null).
- `PUT /trips/{trip_slug}/activities/{activity_id}/status/` — set status (`proposed`|`confirmed`|`completed`) — **`completed` logs a visit entry and snaps a future date back to today**.
- `PUT /trips/{trip_slug}/activities/{activity_id}/rsvp/` — set/clear a **per-activity** RSVP override (falls back to trip-wide RSVP when cleared).

### Trip Comments & Reactions

- `GET /trips/{trip_slug}/comments/` — paginated top-level comments, replies nest one level only — `{id, text, rendered_html, author(masked), image_url, has_map, created, can_delete, reactions[], replies[]}`.
- `POST /trips/{trip_slug}/comments/` — `text`(required), `parent_id?` — image/markup-map attachments are **web-only** — 403 gated by `allow_comments`.
- `DELETE /trips/{trip_slug}/comments/{comment_id}/` — **comment's own author, or the trip's creator** → else 403.
- `PUT /trips/{trip_slug}/comments/{comment_id}/reactions/` — set an explicit target state (not toggle): `{emoji(allowlisted), reacted(bool)}`.

### Trip Calendar Export

- `POST /trips/{trip_slug}/calendar-sync/` — toggle **auto-sync** on an already-exported trip — request: `{enabled}` — **400** (not 409) when no export link exists yet, with a pointer to the export endpoint.
- `POST /trips/{trip_slug}/calendar/` — mirror onto the **caller's own** Google Calendar (each member exports independently; per-exporter visibility setting filters which stops leave the site) — request: `auto_sync?`(presence-keyed) — response: `{calendar: {...}, activities_exported: int}` — own extra throttle bucket (`CalendarExportThrottle`) on top of the standard three — **409** `{error, error_code, authorization_url}` for `calendar_not_connected`/`calendar_reauthorization_required`; `authorization_url` is always the **site's own** connect route, never a raw Google URL — 400 if trip has no dates; 502/503 upstream failures.
- `DELETE /trips/{trip_slug}/calendar/` — remove exported events — response: `{calendar: {...}, removed: bool}` (`removed=false` is **not an error** — idempotent no-op).

---

## Direct Messages & Group Chats

Every `messages:*`-scoped endpoint is **OAuth2-only** — `messages:read`/`messages:write` sit in `permissions.OAUTH2_ONLY_SCOPES`, so a PAT-style `ApiKey` can never reach these, even one hand-edited to carry them. Message-thread endpoints are cursor-paginated (`before=<id>`, `limit` default 50/max 100), `previous`/`count` always null.

### Direct Messages

- `GET /messages/conversations/` — unified inbox, DMs + groups merged, most recent first — `{kind, peer_slug/group_uuid, display_name, unread_count, is_muted, last_message, ...}`.
- `GET /messages/{peer_slug}/` — one page of a 1:1 thread, oldest-first, cursor-paginated — 404 if `peer_slug` doesn't resolve or is reserved (`conversations`, `settings`, `groups`).
- `POST /messages/{peer_slug}/` — send a message, optionally with one `@pin`/`@trip`/`@friend` share — request: `body` or `ciphertext`+`nonce`+`key_version`(never both), `reply_to_id`, `markup_map_id`, `image_ids[]`(max 20, integer pks) and/or `image_uuids[]`(max 20, `Image.uuid` — **preferred for new clients**, additive alongside `image_ids` rather than replacing it), `shared_pin_id`/`shared_trip_slug`/`shared_profile_slug`(exactly one), `client_uuid`(idempotency) — 201/200 replay — pin shares preserve `LocationExposure` provenance. Both attachment fields are scoped to the sender's own not-yet-attached images and may be combined in one request.
- `POST /messages/{peer_slug}/read/` — mark thread read — `{marked_read: <count>}`.
- `POST /messages/{peer_slug}/react/{message_id}/` — toggle emoji reaction — 400 if emoji fails a safety check (relayed verbatim to the other party's client).
- `DELETE /messages/{peer_slug}/messages/{message_id}/` — delete one message — query: `?scope=everyone|self`(default self) — `everyone`(sender only) tombstones for recipient + revokes any carried share; `self`(recipient only) hides just caller's view.
- `GET/PUT/DELETE /messages/{peer_slug}/mute/` — report/set/clear mute, idempotently (not a toggling POST, so a retry lands on the intended state).

### Group Chats

- `GET/POST /messages/groups/` — list caller's groups / create one (`name`, `member_slugs[]`(1-50)) — 400 on unknown slugs.
- `GET/PATCH /messages/groups/{group_uuid}/` — one page of the group thread, cursor-paginated / rename (any active member may) — 404 (not 403) for a non-member.
- `POST /messages/groups/{group_uuid}/messages/` — send into the group.
- `POST /messages/groups/{group_uuid}/read/` — advance read mark to now.
- `GET /messages/groups/{group_uuid}/members/` — list active members, viewer-masked.
- `POST/DELETE /messages/groups/{group_uuid}/members/` — add (**creator-only**, 403 otherwise) / remove members or leave (any member removes self; only creator removes others).
- `POST /messages/groups/{group_uuid}/share/pin/` — share one of caller's own pins into the group — fans out one `PinShare`/`LocationExposure` per connected member.
- `POST /messages/groups/{group_uuid}/messages/{message_id}/react/` — toggle reaction — lookup scoped to the group (message ids sequential across all groups) so a member of one group can't react into another's.
- `DELETE /messages/groups/{group_uuid}/messages/{message_id}/` — delete for everyone, **sender only** — **403 (not 404) for a non-sender** — deliberate exception to the usual 404-everywhere rule since caller already proved membership.
- `POST /messages/groups/{group_uuid}/leave/` — leave (POST not DELETE, mirrors trips/leave) — gated on ever-having-been a member so a retried leave still answers 204.
- `GET/PUT/DELETE /messages/groups/{group_uuid}/mute/` — report/mute/unmute, idempotently.

### Messaging Settings

- `GET/PATCH /messages/settings/` — caller's retention preference (`direct_message_delete_after`) — applies to messages sent from now on only; each message snapshots the setting at send time.

---

## WebSockets

- `WS /ws/notifications/` — scopes: `notifications:read` — server→client live feed of on-site notifications — frames: `{"type":"notification","notification":{...}}` — close codes `4404`(unauthenticated/lacks scope)/`4500`(transient); credential re-validated every 60s.
- `WS /ws/messages/` — scopes: `messages:read` to connect, `messages:write` to send — **OAuth2-only, a PAT can never open this** — bidirectional, broadcasts `dm_message`/`dm_reaction` events — a read-only credential gets an error frame (not a close) when it tries to write.
- `WS /ws/safety/checkin/{checkin_uuid}/chat/` — scopes: `safety:read`/`safety:write` — shared chat + live-location group for owner + ACCEPTED partners — re-validates owner-or-accepted-partner status + credential every 60s, the only mechanism that revokes an already-open connection.
- `WS /ws/safety/contact/{token}/chat/` — scopes: **none** — authorization is the magic-link token, not a credential — never joins the live-location group.

---

## Safety

### Safety Check-ins

- `GET /safety/checkins/` — browse caller's own check-ins, newest deadline first — query: `status=all|active|resolved&trip=<slug>` — owner-scoped only.
- `POST /safety/checkins/` — start a check-in — request: `checkin_by`(future, tz-aware), `grace_period_seconds`(900-604800), `title,plan_details,contact_message`, `destination_latitude`/`longitude`(paired), `trip`(slug, must be joined), `notify_community_wiki`, `contacts[]`(null=saved defaults, []=none), `markup_map` — 409 if an active check-in already exists for that scope.
- `GET /safety/checkins/{checkin_slug}/` — full owner detail — `live_location_*` fields deliberately omitted entirely.
- `PATCH /safety/checkins/{checkin_slug}/` — partial update, the 6 autosave fields the web form has — response includes `{warnings:[...]}` for silently-ignored locked fields — 409 if archived; contacts not editable here.
- `DELETE /safety/checkins/{checkin_slug}/` — stages an Undo History entry.
- `POST /safety/checkins/{checkin_slug}/check-in/` — caller checks in, resolving it — 409 if already resolved.
- `POST /safety/checkins/{checkin_slug}/cancel/` — cancel so it never escalates — 409 if already resolved.
- `POST /safety/checkins/{checkin_slug}/partners/` — invite a partner by username — accept/decline live under `safety/partner-invites/`, not here.
- `DELETE /safety/checkins/{checkin_slug}/partners/{partner_id}/` — remove a partner — also force-closes an accepted partner's open WebSocket.
- `GET/POST /safety/checkins/{checkin_slug}/photos/` — list / attach an already-uploaded image by uuid (not a second upload path).
- `DELETE /safety/checkins/{checkin_slug}/photos/{image_id}/` — delete photo + stored file.
- `GET/POST /safety/checkins/{checkin_slug}/maps/` — primary route map + attached reference maps, standard `{count, next, previous, results:[{uuid, title, is_primary}]}` envelope / attach one of caller's own maps (POST returns the same envelope).
- `DELETE /safety/checkins/{checkin_slug}/maps/{map_uuid}/` — detach a reference map — map itself untouched.
- `GET/PUT /safety/contacts/` — caller's saved default emergency contacts — PUT replaces wholesale (deletes+recreates), not PATCH.
- `GET/PATCH /safety/settings/` — `{default_message,default_grace_period_seconds,auto_delete_after_days}`.
- `GET/PATCH /safety/checkins/{checkin_slug}/location/` — the owner's live position — deliberately its own endpoint, excluded from the check-in detail above so a `safety:read` credential that only syncs check-in metadata never incidentally accumulates a movement trail. GET: viewer-scoped (owner **or** ACCEPTED partner, same rule as chat) — `{sharing_enabled, latitude, longitude, accuracy, updated_at}`, all null when sharing is off. PATCH: **owner only** — a partner gets the same 404 an unrelated caller would, never a 403 — `{sharing_enabled?, latitude?, longitude?, accuracy?}`; `latitude`/`longitude` must arrive together; turning sharing on and reporting the first fix works in one call; turning sharing off clears the last-known position rather than leaving a stale marker. Own rate-limit bucket (`external_api_safety_location`, 360/hour) since one-fix-per-10s would exhaust the standard write cap in under an hour. **Residual risk:** a leaked `safety:read` credential becomes a live tracker for every check-in its holder partners on, for as long as that check-in stays active and sharing stays on — the same exposure a connected browser tab already carries via the WebSocket location group, not a new capability.

### Safety Check-in Chat

- `GET /safety/checkins/{checkin_slug}/messages/` — shared chat transcript, newest first, paginated — viewer-scoped (owner **or** ACCEPTED partner), not owner-only; emergency contacts excluded (tokenized portal only); `token` field never exposed.
- `POST /safety/checkins/{checkin_slug}/messages/` — post a chat message — 409 once archived (PII sealed); REST-sent messages reach open WebSockets in real time.

### Safety Partners

- `GET /safety/partner-invites/` — caller's own pending partner invitations — the only route by which an invitee learns a check-in's uuid.
- `POST /safety/partner-invites/{checkin_uuid}/accept/` — idempotent (conditional UPDATE).
- `POST /safety/partner-invites/{checkin_uuid}/decline/` — decline, or resign an already-ACCEPTED partnership — **not idempotent**: deletes the row, repeat is genuine 404; also revokes the resigning partner's live socket.
- `GET /safety/partner-checkins/` — check-ins caller watches as ACCEPTED partner — merely-INVITED rows excluded.
- `GET /safety/partner-checkins/{checkin_uuid}/` — **same full detail shape the owner sees** minus edit controls, plus `owner_username`/`owner_profile_uuid` — 404 if not an accepted partner.
- `POST /safety/partner-checkins/{checkin_uuid}/mark-safe/` — partner confirms owner is safe, resolving the check-in — 409 if already resolved by someone else (conditional UPDATE prevents a double-resolve race).

**Addressing note:** the chat route is addressed by check-in **slug**; every partner-facing route is addressed by **uuid only** (a slug is unique per-owner, not globally). Every refusal on both surfaces — including "doesn't exist" — is 404, never 403, since a 403 on a safety identifier would itself disclose that a specific person is out somewhere right now.

---

## Device Scanning

The mobile app's wireless-device-scanning feature (Wi-Fi/Bluetooth devices detected while walking a
route, so a user can notice a camera/sensor/tracker they didn't expect) uploads raw scan data for
background classification and clustering. **This API never reads that raw data back — there is no
endpoint, here or on the internal session-authenticated `/rest/` surface, that returns an individual
scan, upload, or reading, or who submitted it.** The only thing ever queryable is the cumulative,
unattributed result: a fuzzy `WikiDeviceMarker` location per (device, wiki), which carries no
`profile`/uploader reference at all — even when scans from several different accounts corroborate
the same marker, nothing in its stored fields or its API representation distinguishes one
contributor from another. Markers are also only ever returned for wikis the caller has already
discovered, via the same `wiki_access` visibility gate every other wiki-scoped read in this API uses.

`POST /device-scans/` — `DeviceScanUploadView` — scopes: `device_scans:write` — upload one batch from a walked route. Request: `client_session_uuid?`, `devices: [{mac_address, device_name?, device_type_guess?("camera"|"sensor"|"tracker"|"access_point"|"phone"|"wearable"|"iot"|"other"|null), detected(default true), estimated_latitude, estimated_longitude, expected_marker_uuid?(from a prior `nearby/` response, confirms/refutes that marker), readings?: [{latitude, longitude, signal_strength?, observed_at}]}]` (≤200 devices/upload, ≤500 readings/device) — response **202**: `{upload_uuid}` — classification, wiki-matching, and marker clustering all happen afterward in the background (`dashboard.tasks.process_device_scan_upload`); this response describes nothing about what was found, and nothing about it is ever readable back through this API. Attribution to the caller's account is controlled entirely by `Profile.track_device_scans` (see [Account & Identity](#account--identity)) — authentication is always required regardless of this preference, but the stored data carries no profile reference when it's off; either way the upload is fully processed, since classification and the community marker it may produce don't need an owner. Camera/sensor/tracker detections update a fuzzy marker on every wiki (including child wikis) whose boundary contains the reported coordinates, possibly several or none; other device types are recorded but never raise a marker.

`GET /device-scans/nearby/` — `NearbyDeviceMarkersView` — scopes: `device_scans:read` — devices already known nearby, so the app can decide when to turn scanning on or enrich what it shows on a live detection. Query: `latitude`, `longitude`, `radius_meters?`(default 500, max 50000). Response: `{markers: [{marker_uuid, device: {mac_address, device_type, display_name}, latitude, longitude, radius_meters, confidence, avg_signal_strength, last_observed_at, status("active"|"stale")}]}` — `presumed_removed`/`dismissed` markers are never returned, and no field here ever identifies a contributor. The fuzzy area (`radius_meters`) shrinks and `confidence` rises as more scans corroborate the same location over time, weighted toward recent activity; a device that appears to have moved shows up as two separate markers until the old one goes stale. A caller reporting `expected_marker_uuid` with `detected: false` on a later upload feeds an absence streak that eventually flips a marker to `presumed_removed`.

---

## Friends & Social

### Friends & Friend Requests

- `GET /friends/` — list caller's friend relationships — query: `status`(default `Accepted`), cursor pagination — **`status`/`relationship_type` wire values are capitalized** (`"Accepted"`, `"Requested"`) — the only capitalized enum on this whole surface.
- `POST /friends/` — send a friend request (or auto-accept one already pending in reverse) — request: `profile_uuid`, `message?`.
- `DELETE /friends/{profile_uuid}/` — end an existing friendship.
- `POST /friends/{profile_uuid}/accept/` `/reject/` `/ignore/` — accept, decline (may re-send later), or silently+permanently ignore (requester can never re-send).
- `POST /friend-invites/` — invite a non-friend by email — response **always** `{"result": "sent"}` regardless of registered/unregistered/privacy-rejected/mail-failure (anti-enumeration).

### Friend Moderation (block/mute)

- `POST /friends/{profile_uuid}/block/` — block, creating the row if none exists — the only transition that works against a total stranger.
- `POST /friends/{profile_uuid}/unblock/` — lift caller's own block — resulting status is `Removed`(not deleted).
- `POST /friends/{profile_uuid}/mute/` — **deprecated** alias for `{"is_muted": true}`; cannot unmute.
- `PATCH /friends/{profile_uuid}/mute/` — set mute to explicit target state (not a toggle) — `{"is_muted": bool}`. **Known limitations:** (1) `is_muted` lives on the shared `Friendship` row — **not per-viewer**, label it "muted" never "muted by you"; (2) **does not currently suppress any notification delivery** — tracked in `docs/PROBLEMS.md`. `DirectMessageMute` and per-group-chat mute are unrelated and do work.

### Profiles

- `GET /profiles/{profile_slug}/` — scopes: `profile:read` **and** `social:read` (both required) — accepts slug or uuid — 404 (never 403) when excluded by `profile_visibility`.
- `PATCH /profiles/{profile_slug}/` — update caller's **own** profile only — fields: `bio, area, started_exploring` only. **`theme_mode`/`distance_units`/`community_enabled`/the twelve `*_visibility` fields live on `PATCH /settings/` instead** (moved there after a `social:write`-only credential was able to rewrite privacy fields through this endpoint).
- `GET/POST /profiles/{profile_slug}/notes/`, `PATCH/DELETE .../notes/{note_uuid}/` — caller's own private notes about the subject — never visible to the subject, even on their own profile.

### Profile Social Extras

- `PUT/DELETE /profiles/{profile_slug}/avatar/` — replace/clear caller's own avatar (multipart) — gated on `social:write` not `photos:write` (avatar is a `Profile` field, not an `Image` row).
- `POST /profiles/{profile_slug}/avatar/emoji/` — generate an emoji avatar (`animal`, `color`, closed sets) — Gravatar generation deliberately **not** exposed here.
- `GET /profiles/{profile_slug}/annotations/` — combined read of nickname/trust/note_count — **read-only by design**.
- `PUT/DELETE /profiles/{profile_slug}/nickname/` — set/clear private nickname — PUT blank **not allowed** (use DELETE to clear).
- `PUT/DELETE /profiles/{profile_slug}/trust/` — set/clear private trust rating (1-5).
- All annotation/nickname/trust rows are strictly private to their author.
- `GET/PUT /profiles/{profile_slug}/social-links/` — a profile's public social links (Instagram, Bluesky, Discord, UER, Facebook, Flickr, YouTube, TikTok, Reddit, or a generic website) — scopes: `profile:read`+`social:read` / `social:write`. GET carries **no separate contact-visibility gate**, unlike phone/Discord/etc. under `contact` — anyone who can see the profile at all (the same `profile_visibility` check every other route here uses) sees its links, matching the public profile page. PUT is a **full replace** (deletes+recreates the whole set, like `/safety/contacts/` — there's no per-entry addressing to PATCH against) — request: `{links: [{platform, handle}, ...]}`; `handle` means a username for the eight handle-based platforms (validated against the same rules a pasted profile URL is checked against on the web), Discord's own free-form username (charset-checked, no public profile URL to parse), or the full URL itself for `website` — 400 on an invalid handle/URL, an unknown platform, or a platform repeated in the same request — response (both verbs): `{links: [{platform, handle, url(null for discord), display_name, icon}]}`. Own profile only; any other slug is 404, never 403. Distinct from `discord_username` under [Account & Identity](#account--identity) — that's a private contact field, this is a public link, and they can hold different values.

---

## Notifications & Push

### Notifications

- `GET /notifications/` — one page of caller's inbox, newest first — query: `unread_only`, cursor pagination — `source_profile` masked, null for system/safety/error notifications with no triggering person.
- `POST /notifications/{notification_uuid}/` — mark one read — response: 204 **always**, whether or not a matching row existed (idempotent + anti-enumeration).
- `POST /notifications/read-all/` — clear all unread.
- `GET /notifications/unread-count/` — badge-count-only, split out so polling doesn't page through bodies.
- `GET/PATCH /notification-preferences/` — per-type delivery matrix (`{delivery, whatsapp, sms}` per type) — `whatsapp`/`sms` forced off server-side when profile has no phone number.

### Push Devices

- `POST /push-devices/` — register/re-activate this device — `address` never echoed back — idempotent on submitted address (safe to re-register every app launch).
- `DELETE /push-devices/{device_uuid}/` — unregister.

---

## Games

`GET /games/spotguessr/` — `SpotGuessrOverviewView` — scopes: `games:read`(+`social:read` for `friend_ratings`) — start-screen data: modes, limits, saved config, own rating, resumable session, friend ratings (masked for restricted friends).

`PATCH /games/spotguessr/preferences/` — `SpotGuessrPreferencesView` — scopes: `games:write` — body: `show_ratings_to_friends` (bool) — the only genuinely user-editable SpotGuessr preference; `last_config` stays read-only here (auto-managed from every session start) — response echoes the saved value.

`GET /games/spotguessr/eligible-count/?geo_bounds={geojson}` — `SpotGuessrEligibleCountView` — scopes: `games:read` — count of the caller's own pins inside a candidate `geo_bounds` polygon (required) — a lightweight pre-check before spending the `GameStartThrottle` budget on a config with nothing to play — 400 for missing/malformed `geo_bounds` (never a 500).

`GET /games/spotguessr/eligible-pins/?geo_bounds={geojson}` — `SpotGuessrEligiblePinsView` — scopes: `games:read` — paginated (`{count,next,previous,results}`) feed of the caller's own pins as candidate SpotGuessr locations (`label`, `latitude`, `longitude`), optionally narrowed by the same `geo_bounds` param — a browse/read endpoint, not a play mode; solo eligibility is exactly "the player's own pinned locations".

`GET/POST /games/spotguessr/sessions/` — `SpotGuessrSessionsView` — scopes: `games:read`/`games:write` — list caller's session history / start a solo session + generate its first round — POST request: `mode`(default photos), `total_rounds`, `difficulty`, `allow_arbitrary_external_photos`, `require_visited_all`, `date_guessing_enabled`, `use_aliases`(default true), `round_time_limit_seconds`, `geo_bounds`(GeoJSON), `label_id`(restrict to spots tagged with this label or a descendant; must resolve to a label visible to the caller, else 400) — 201 or 409 `{error_code:"no_eligible_locations"}` — **own extra throttle** `GameStartThrottle`(40/hour) stacked on the standard three (round generation runs up to 25 eligibility passes + a possible billed Street View call).

`GET /games/spotguessr/sessions/{session_id}/` — scopes: `games:read` — resume-state row — 404 if not participant (never 403); 409 `multiplayer_unsupported` for a LOBBY/multi-participant session (solo-only surface).

`GET /games/spotguessr/sessions/{session_id}/round/` — scopes: `games:read` — return the round to play now, generating the next round or completing the session as a side effect — response is one of `{finished:false, round:{...}}`, `{finished:true, summary:{...}}`, or `{finished:false, no_eligible_locations:true}` — **not idempotent/cacheable despite being a GET**; throttle tier forced to `write`.

`POST /games/spotguessr/sessions/{session_id}/rounds/{round_id}/guess/` — scopes: `games:write` — submit `latitude`, `longitude`, optional `guessed_date` — response includes `distance_meters, points, date_points, bonus_points, rating_delta` and answer fields **only once genuinely revealed** — row-locked against concurrent double-submit.

`GET /games/spotguessr/sessions/{session_id}/summary/` — scopes: `games:read` — final scoreboard incl. rating movement, per-participant totals.

`POST /games/spotguessr/sessions/{session_id}/rounds/{round_id}/expire/` — `SpotGuessrRoundExpireView` — scopes: `games:write` — client-driven fast path for "the round timer hit zero"; the authoritative check is still server-side (`round.created` + the session's `round_time_limit_seconds`, never the client's clock) — response `{"revealed": bool}` — a no-op, not an error, when the round is already revealed or the timer genuinely hasn't expired yet.

`POST /games/spotguessr/sessions/{session_id}/rounds/{round_id}/feedback/` — `SpotGuessrRoundFeedbackView` — scopes: `games:write` — body: `kind` (`thumbs_up`/`thumbs_down`/`reported`) — records (or overwrites) the caller's reaction to a Photos-mode round's photo, feeding `services.media.media_relevance` — 403 if the caller never guessed on this round; 400 for a round with no photo (Named Place/Street View).

`GET /games/spotguessr/sessions/{session_id}/rounds/{round_id}/image/` — `SpotGuessrRoundImageView` — scopes: **`games:read` AND `media:read`** (both required) — round photo as raw bytes with **all EXIF metadata stripped** (source photo routinely carries GPS tags pointing at the answer) — `Cache-Control: private, max-age=300` — metered against the **media** throttle bucket (`ExternalApiMediaThrottle`), not the JSON read/write budgets.

---

## Undo History

A thin, scope-aware wrapper over the existing Undo History service (`services.undo`) that already backs pin/wiki/trip/saved-filter/safety-checkin deletes on the website — nothing new was built at the model layer.

`GET /undo/` — `UndoListView` — scopes: `undo:read` — the caller's active (non-expired) undo entries across every undoable model, newest first. Aggregating across model types means a credential can hold `undo:read` without holding every domain's own read scope — an entry is **omitted, not 403'd**, when the caller lacks that model_label's paired domain-read scope (`pin`→`pins:read`, `wiki`→`wiki:read`, `trip`→`trips:read`, `saved_filter`→`lists:read`, `safety_checkin`→`safety:read`). Response: `{entries: [{uuid, model_label, object_repr, created, expires_at}], omitted: [model_label, ...]}` — a client should prompt the user to re-authorize when `omitted` is non-empty rather than silently rendering a forever-incomplete list.

`POST /undo/{uuid}/restore/` — `UndoRestoreView` — scopes: **`undo:write` AND the entry's own domain write scope** (restoring a delete needs the same authority the delete itself needed) — a missing scope, an unknown uuid, or another profile's uuid are all **404**, identically — never 403. **410 Gone** `{"error": "This undo entry has expired."}` when the entry is past its 7-day retention window — deliberately distinguished from 404 ("never yours to begin with"), since the entry genuinely existed; the stale row is deleted on this attempt. Success: **200** `{"restored": true}`.

---

## Search

`GET /search/` — `GlobalSearchView` — scopes: `search:read`(floor only — each result-type section additionally requires its own domain scope) — cross-domain search over pins, photos, wikis, articles, trips, visits, messages, maps, safety check-ins, comments. Query: `q`(optional, blank returns `total:0` not a 400), `types`(comma-separated, unrecognized dropped), `limit`(1-50/section) — response: `{query, total, used_fallback, filter_chips[], errors[], omitted_types[], groups:[{type,label,icon,results:[...]}]}` — **grouped, not paginated** — own extra throttle `GlobalSearchThrottle`(300/hour) stacked on the standard three.

**Per-section scope gating**: each section needs `search:read` **plus** its own scope — `pins`→`pins:read`, `photos`→`photos:read`, `wikis`/`articles`→`wiki:read`, `trips`→`trips:read`, `visits`→`visits:read`, `messages`→`messages:read`(**OAuth2-only** — a PAT can never reach DM search results), `maps`/`comments`→`pins:read`, `safety`→`safety:read`. A denied section is dropped from the provider chain before any query runs (never a 403 for the whole call) and its slug appears in `omitted_types[]` — a section *absent from `groups` but present in `omitted_types`* means "not authorized"; present with empty `results` means "authorized, no matches."

---

## AI Assistant

`POST /assistant/message/` — `AssistantMessageView` — scopes: `assistant:write` — one chat turn against the same tool-calling assistant (UL-293) the website's session-based chat uses (`services.ai.assistant.run_assistant_turn`), but **stateless**: a bearer-token client has no session to keep history in, so the client carries `history` in the request body and resends the `history` this endpoint returns as the next call's input. Request: `{message, history: [{role:"user"|"assistant", content}]}` — `history` capped to the last 20 entries server-side both on the way in and the way out. Response: `{reply, actions[], history}` — `actions` are human-readable labels of anything the assistant actually did (e.g. "Created a trip"); it can only act through a small allowlisted tool set scoped to the caller's own data, never deletes/shares/changes privacy settings, and every tool result it sees is JSON, never raw prose from another account. **503** `{"error": "AI features are currently turned off for your account or this site."}` when AI is disabled — own extra throttle (`external_api_assistant_message`, default 60/hour) stacked on the standard three, since one turn can fan out to several billed model calls.

`POST /assistant/reset/` — `AssistantResetView` — scopes: `assistant:write` — a genuine no-op for this stateless shape (`{"history": []}`) — kept for surface symmetry with the web chat's reset button; a client "resets" by simply discarding its own `history` and sending an empty list next time.

---

## End-to-End Encryption (E2EE)

Mounted at `dashboard/e2ee/` (not under `api/external/v1/`, but published in the same schema and reachable by the same credentials — `DualAuthJsonView` accepts session cookie **or** bearer token). There is deliberately **one** implementation serving both the website and the mobile app — no separate copy exists under `api/external/`, because a drift between two copies of a key-exchange contract means someone's messages stop decrypting. Every write here requires `current_password` even for an OAuth2 caller on accounts that have one — a bearer token alone must never be sufficient to replace an account's key material.

`GET /dashboard/e2ee/login-params/` — `E2EELoginParamsView` — **anonymous, unthrottled** — reports how a login identifier authenticates (`{mode: "derived"|"legacy", auth_salt}`) so the login form can pick its flow before anyone is authenticated — unknown identifiers get a deterministic decoy salt, indistinguishable from a real enrolled account.

`POST /dashboard/e2ee/enroll/` — `E2EEEnrollView` — scopes: `messages:write` — publish the caller's first key bundle, optionally rotating the login credential to derived auth in the same call — request: `public_key`, `recovery_wrapped_secret`, optional `password_wrapped_secret`+`password_wrap_salt`, optional `auth_key`+`auth_salt`+`current_password` — 201; 409 if a bundle already exists; 403 on bad password proof.

`GET /dashboard/e2ee/keys/` — `E2EEOwnKeysView` — scopes: `messages:read` — the caller's full bundle including wrapped blobs — returns `{"enrolled": false}` as a normal 200 (not 404) when not yet enrolled, since it's polled on every page load for the encryption-status indicator.

`GET /dashboard/e2ee/keys/{profile_slug}/` — `E2EEPartnerKeyView` — scopes: `messages:read` — a conversation partner's public key only — response: `{public_key, version}` — 404 when the partner has no bundle or no DM relationship is permitted in either direction.

`GET/POST /dashboard/e2ee/conversation-key/{profile_slug}/` — `E2EEConversationKeyView` — scopes: `messages:read`/`messages:write` — GET returns the caller's wrapped copy of every key version for the pair (`{keys:[{version,wrapped_key}], latest}`) — existing keys are always returned regardless of the *current* relationship, so a participant keeps the ability to decrypt history even after a block. POST stores the next version (`version`, `wrapped_for_me`, `wrapped_for_partner`) — 409 on a version mismatch (expected value returned for retry) or if either participant isn't enrolled; a concurrent-create race returns the winner's copy at 200 instead of erroring.

`GET/POST /dashboard/e2ee/group-key/{group_uuid}/` — `E2EEGroupKeyView` — scopes: `messages:read`/`messages:write` — GET returns the caller's own envelopes plus rotation state; `members[].id` is an **opaque per-(group,member) token**, never a profile slug (so the payload can't reveal identities masked elsewhere) — clients must round-trip these tokens verbatim in the POST's `wrapped` mapping, which is rejected 409 if it doesn't cover the group's active membership exactly (e.g. after someone joins/leaves).

`POST /dashboard/e2ee/rewrap/` — `E2EERewrapView` — scopes: `messages:write` — replace wrapped copies of the caller's *existing* private key (used after a password reset, or to regenerate the recovery key) — request: optional `password_wrapped_secret`+`password_wrap_salt` and/or `recovery_wrapped_secret` — requires `current_password` when re-wrapping under a password.

`GET /dashboard/e2ee/rewrap-all/` — `E2EERewrapAllView` — scopes: `messages:read` — every sealed conversation-key/group-envelope copy addressed to the caller in one call, for bulk client-side re-wrap ahead of a reset (avoids N round trips) — 404 if not enrolled.

`POST /dashboard/e2ee/reset/` — `E2EEResetView` — scopes: `messages:write` — **last resort**: replace the caller's keypair entirely — requires literal `confirm: "RESET"` plus `current_password` where applicable — optionally accepts `rewrapped_conversation_keys`/`rewrapped_group_envelopes` (re-sealed under the *new* key) so history stays readable; without them, old messages become permanently unreadable to the caller (partners keep their own copies) — all-or-nothing atomic transaction; any id not owned by the caller rejects the whole request before writing anything.

`POST /dashboard/e2ee/change-password/` — `E2EEChangePasswordView` — **session-only, deliberately not dual-auth** — the one endpoint that calls `user.set_password()` against the account's login credential; exposing it to a scoped `messages:write` OAuth2 token would let a leaked chat-sending token take over the whole account. A mobile client needing this sends the user through the web flow.

---

## Not Yet Implemented

The following `urls_*.py` modules exist under `external_api/` purely as placeholders — each is a docstring plus an empty `urlpatterns = []`, with no backing views and no reserved scopes in `ApiKeyScope`. Nothing below is reachable on this branch; treat any assumption otherwise as wrong.

- **Connections** (`urls_connections.py`) — Immich/OAuth-identity-provider/plugin-backed connect-disconnect-status-resync flows. Docstring flags that a status endpoint must never echo a stored `EncryptedTextField` secret back through the API. Deferred, decision pending — see D7 in `docs/notes/mobile_app_notes.md`.
- **Memories, extra** (`urls_memories.py`) — now carries the timeline and on-this-day routes (see [Memories](#memories) above), plus the pre-existing `GET /memories/journal/` which predates this split. Dismissible memory cards remain unscheduled P2.
- **Site** (`urls_site.py`) — **will not be exposed to the mobile app.** Site settings/quotas/feature flags, announcements, version/health, and staff-only site-admin ops are not part of this API's contract, now or planned — this module stays an empty placeholder by decision, not by scheduling.
- **Tools** (`urls_tools.py`) — **import/export (KML/GPX/CSV) will not be built for the mobile app at this time**, though it may be revisited in the distant future; bulk edits and map/geometry helpers remain unscheduled P2. **Undo/restore already lives in this module** (`GET /undo/`, `POST /undo/{uuid}/restore/`) — see [Undo History](#undo-history) — it just isn't one of the "tools" the module's own docstring originally had in mind.

Custom Fields, Panels, and the AI Assistant (formerly listed here) are implemented — see their own sections above.
