The following list was provided by an agent working on the mobile app, providing input on what changes UrbanLens needs to make in order to allow the mobile app to work correctly, with full parity with the existing web app features.

Don't take their suggestions as gospel. Our codebase here is more mature than theirs, and is the basis of what we're trying to do when there is a conflict. However, when the requests made are consistent with our goals and can be implemented safely, securely, maintainably, extensibly, and performantly, we should implement them. 

If any of these are not achievable within those constraints, create a note of that in docs/notes/mobile_app_notes.md

# Required UrbanLens Server Changes

The app makes **no modifications** to the UrbanLens repository. This document is the
**current, outstanding** list of server-side work the app depends on, in priority order.
It follows the server's own `docs/external_app_api_plan.md` and
`docs/designs/mobile-app-stack-r2.md`: a dedicated `dashboard/external_api/` app mounted
at `/dashboard/api/external/v1/`, scoped credentials (PAT `ulk_…` keys and OAuth2+PKCE
tokens), thin serializers calling the existing service layer, per-credential throttling,
and usage logging.

Legend: **[P0]** app is unusable against a real server without it · **[P1]** major
feature gap · **[P2]** parity polish.

## Status as of 2026-07-27

As of server branch `feature/external-api-mobile-v2`, `dashboard/external_api/` is
substantially live: 116 endpoints across pins, lists/saved-filters/labels, wikis, trips,
messaging, photos/memories, safety check-ins, and social/notifications (server team's own
summary: `docs/notes/mobile_app_notes.md`). This document was fully reconciled against
that branch's actual `serializers.py`/`serializers_wiki.py`/`serializers_messaging.py`/
`views*.py` on 2026-07-27 — every item below is either a genuinely still-open ask or a
noted design question, not a stale guess. Everything confirmed *shipped and correct* has
been removed from this list; the app's own `lib/data/api/api_*_repository.dart` files
carry dated `CONFIRMED`/`CONFIRMED ABSENT` comments citing the exact serializer/field
checked, if you want the app-side detail behind any item here.

A few systemic things worth knowing before reading the per-domain lists:

- **Every sub-resource is keyed by its own identifier, not always the parent's.** Trip
  members/comments nest their author as `{uuid, slug (null when masked), display_name,
  avatar_url}`; friendship rows have **no id field of their own at all** — every friend
  action addresses the *other profile's uuid* directly. Worth double-checking client
  assumptions on any future endpoint that returns a person.
- **Identity masking is inconsistent across surfaces.** `FriendProfileSerializer` exposes
  an explicit `is_masked` boolean; `Conversation`/`GroupMember` expose `is_anonymized`;
  `TripMember`/`TripComment`/`DirectMessage` bake the masking directly into
  `display_name`/`slug` with no separate flag at all. Not necessarily wrong, just
  something to keep consistent going forward.
- **PATCH endpoints are often narrower than the model.** `PinUpdateSerializer` only
  writes `name`/`icon`/`last_visited`/`latitude`/`longitude`/`parent_id`; `WikiUpdateSerializer`
  only writes `name`/`description`/dates/`security`; `ProfileUpdateSerializer` doesn't
  accept `display_name`. If any of these are supposed to be editable and aren't, see the
  per-domain notes below.

---

## 1. Authentication [P1]

- **[P1] First-party OAuth2 client registration.** Please confirm a public OAuth2
  application (no secret, PKCE required) is registered with `client_id: urbanlens-app`
  (or tell us the assigned id — it's one constant, `AppConfig.oauthClientId`), redirect
  URI `urbanlens://oauth/callback`, grant type authorization-code, token endpoint auth
  none. Not verified against a live server this pass.
- **[P2] E2EE login-params.** Please confirm `/dashboard/e2ee/login-params/` stays public
  JSON alongside the OAuth2 flow — not re-checked this pass.
- **[P2] Token introspection.** No `GET`-scopes-and-expiry endpoint is confirmed for the
  settings screen (`auth/session/` exists but returns `{user_id, session_key,
  csrf_token}` — a session-echo, not an OAuth2 token introspection response).

*(Resolved, no action needed: per-domain scopes — `LISTS_READ/WRITE`, `TRIPS_READ/WRITE`,
`SAFETY_READ/WRITE`, `MESSAGES_READ/WRITE`, etc. — all exist and are enforced per-endpoint,
confirmed by reading the views directly. PAT keys deliberately still default to the
original four scopes only, which the server team has already told us is an intentional
security decision, not an oversight.)*

## 2. Pins & Locations [P0/P1]

- **[P0] `PATCH /pins/{slug}/` only persists `name`/`icon`/`last_visited`/`latitude`/
  `longitude`/`parent_id`.** `description`, `pin_type`, `color`, `address`, `priority`,
  `danger`, `vulnerability`, `rating`, `visited`, `label_ids`, the three date fields, and
  `security` all have **no write path at all** — editing any of them in the app currently
  silently no-ops against a real server. This is the sharpened version of the original
  "extend PATCH" ask: please confirm whether this narrow field set is intentional, or
  widen it to match `PinCreateSerializer`'s writable set plus label/security/date
  editing.
- **[P1] Pin comments have no reactions endpoint.** Wiki comments do
  (`PUT/DELETE .../comments/{id}/reactions/{emoji}/`) — pins don't. Asymmetric; please
  add the equivalent for pins, or confirm it's intentionally DM/wiki-only.
- **[P1] Pin↔wiki manual sync** (`send-to-wiki`/`pull-from-wiki` bulk actions,
  `dashboard/services/pins/pin_wiki_sync.py`) has no external-API mirror — still session/HTMX
  only.
- **[P1] Pin's own private article** (`Article.pin` OneToOne, real model, real internal
  save/preview/history HTMX views) has no external-API mirror.
- **[P2] Parcel/building-footprint lookup** — no external-API surface exposing the
  server's county-GIS/CRIS/OSM building-footprint cache per parcel.
- **[P2] Bulk ops** (`POST /pins/bulk/` — edit/delete/merge/label) — no endpoint.
- **[P2] City/state/country as separate pin fields** — only a combined `address` string
  is exposed; `Location` does derive them, just not on the wire.
- **[P2] Pin notes have no reactions or create-time threading** (`parent_id`) — flat and
  append-only by design server-side. Fine if intentional; flagging in case threading was
  meant to ship here too (trip comments *do* support `parent_id`).
- **[P2] Visits have no update/PATCH endpoint** — only create + delete. The app now edits
  by delete-and-recreate; a real PATCH would be cleaner if worth adding.

*(Resolved: sync feed, tombstones, idempotent create with `parent_id`, the full detail
endpoint — including description/dates/security/boundary/notes/aliases/links/embedded
custom_fields/cover_photo_url/wiki_slug — and every sub-resource (notes, aliases incl.
"use this name", links, visits, comments, review) are all live and confirmed correct.)*

## 3. Lists, Saved Filters, Labels [P1/P2]

- **[P2] Lists have no way to link a snapshot markup map.** No `markup_map` field on
  `PinListWriteSerializer`, no dedicated endpoint. (LIST.MARKUPMAP-01 works in demo mode
  only until this exists.)
- **[P2] Label reorder, bulk-delete, bulk-edit, bulk-convert** — none has a REST v1
  endpoint; only the per-kind dashboard routes exist.
- **[P2] Priority (cross-kind) reorder** — only reachable via the dashboard's own
  `POST /organize/priority/save/`, no REST v1 equivalent; please confirm that route is
  actually reachable through this client's base URL/auth, or add a REST mirror.

*(Resolved: full list CRUD + items add/remove/reorder + resync, saved-filters CRUD, and
label CRUD + merge + per-profile customization are all live and confirmed correct — note
`source_uuids`/`parent_uuids` are the real field names, not `*_ids`.)*

## 4. Wikis & Community [P1/P2]

- **[P1] Wiki alias "use this name" action** — pins have
  `POST /pins/{slug}/aliases/{id}/use/`; wikis have no equivalent at all.
- **[P2] Wiki alias nickname-toggle** — no endpoint (the app's own
  `toggleAliasNickname` feature has no server-side counterpart to call).
- **[P2] Wiki ownership/sale-history** (`PropertyOwner`/`PropertySale`) — no endpoints.
- **[P2] Wiki public-vote ("make this location public") ballots** — no endpoints for
  casting/withdrawing.
- **[P2] Wiki boundary set/clear** — no endpoints (pins have their own boundary in the
  detail payload; wikis have nothing comparable exposed).
- **[P2] Wiki detail-pins CRUD** — no endpoints for the community "detail pins" list.
- **[P2] Wiki cover photo isn't PATCH-writable** — `cover_photo_url` isn't in
  `WikiUpdateSerializer`'s field set.
- **[P2] Wiki edit-history hard-delete/expunge** — only `.../history/{id}/revert/`
  exists, no `.../delete/`.

*(Resolved: wiki detail GET/PATCH — name/description/date_abandoned/date_last_active
(bare `YYYY-MM-DD`, not datetime)/security, all confirmed — history + revert, stat votes
(`GET/PUT/DELETE .../votes/{field}/`, values 1-5, DELETE clears), aliases + links CRUD,
gallery, article + revisions + `base_revision_id` conflict detection, comments +
reactions, and `pin_count_low`/`pin_count_approx`/`first_pinned` are all live and
confirmed correct. Discovery-gating via `resolve_visible_wiki()` is confirmed intact on
the GET route. Wiki-gallery photo voting works, but through the generic
`/photos/{uuid}/vote/` endpoint (§7) — not a wiki-scoped one.)*

## 5. Trips [P1]

- **[P1] Trip settings have no write path at all.** `TripPermissionsSerializer`'s four
  fields (`allow_add_members`/`allow_add_activities`/`allow_edit_activities`/
  `allow_comments`) are all `read_only=True` on the external API — the site's own
  `POST /trips/<slug>/settings/` (session/HTMX) is the only place they're actually
  editable. Please add a REST v1 write path.
- **[P1] No JSON path to actually export a trip to Google Calendar or remove an export.**
  The only real calendar endpoint, `POST /trips/{slug}/calendar-sync/ {enabled}`, only
  toggles auto-sync on a trip **already** exported — and that export only happens through
  the website's own OAuth-redirect flow. There's no way to trigger the export itself, or
  undo it, from a JSON client.
- **[P2] `calendar_account_email` isn't exposed anywhere** on the trip payload (only
  `connected`/`linked`/`auto_sync`/`last_synced`).

*(Resolved: full CRUD (blank name gets an auto-generated placeholder, confirmed), join/
leave/rsvp (PUT, wire values `yes`/`no`/`maybe`, null clears), members add/remove/
organizer-toggle (PATCH with an explicit `is_organizer` target, not a toggle), activities
CRUD + position + vote (`up`/`down`/null string) + status + rsvp, map, and comments +
reactions (`PUT {emoji, reacted}`, explicit target not a toggle) are all live and
confirmed correct.)*

## 6. Messaging & E2EE [P0/P1]

- **[P0] E2EE key endpoints are still 100% session-cookie-gated.** `enroll`/`keys`/
  `conversation-key`/`group-key`/`rewrap`/`reset` all remain under `/dashboard/e2ee/…`
  only — confirmed by grepping both that mount and `dashboard/external_api/` directly,
  nothing E2EE-related reached the external API on this branch. This is the single
  biggest remaining gap: it blocks "the server never sees plaintext" for any API-mode
  (PAT/OAuth2) user. Please either accept `ApiKeyAuthentication` on the existing e2ee
  views, or mirror them under `/api/external/v1/e2ee/…`.
- **[P1] Group message reactions** — no endpoint (DM reactions work fine).
- **[P1] Group message delete** — no endpoint (DM delete works fine).
- **[P1] Group leave** — no dedicated route. It's really just
  `DELETE /messages/groups/{id}/members/ {member_slugs: [<your own slug>]}`, but a bare
  API credential has no way to learn its own profile slug (`whoami/` only returns
  `{uuid}`) — either add a `/leave/` route, or add `slug` to `whoami/`'s response.
- **[P1] Pin-share accept/reject** — a pin can be shared into a message
  (`shared_pin_id`), but there's no endpoint for the recipient to record accept/reject.
- **[P1] Per-conversation mute** — no endpoint; `is_muted` is read-only on the
  conversation list row.
- **[P1] Per-conversation disappearing-messages** — no endpoint. Only one **account-wide**
  retention setting exists (`GET/PATCH /messages/settings/
  {direct_message_delete_after}`), not per-thread.
- **[P2] Message image attachments want an int, not the uuid the rest of the API uses.**
  `MessageSendSerializer.image_ids` wants a list of `Image` row integer pks, but
  `/photos/` is uuid-addressed everywhere else — there's no confirmed way to resolve one
  to the other. Please either accept photo uuids here too, or expose the integer id
  somewhere reachable.

*(Resolved: conversations list (`kind`/`peer_slug`/`group_uuid`/`display_name`/
`creator_slug`, confirmed exactly as assumed), DM history + send with full E2EE transport
fields (`ciphertext`/`nonce`/`key_version`) and all three share-reference types, read
receipts, DM reactions (`POST .../react/{id}/ {emoji}`, toggle), group create/rename/
list/detail/add-members/messages/read/share-pin, and message settings are all live and
confirmed correct.)*

## 7. Photos & Memories [P1/P2]

- **[P2] Photo custom fields** — no REST endpoint, and unlike pins, not embedded in the
  base `/photos/` payload either.
- **[P2] Memories timeline/on-this-day feeds** — not confirmed to exist; only
  `/memories/journal/` was confirmed real this pass. Please confirm or add.
- **[P2] A "new pin" suggestions feed** (as opposed to the confirmed-real
  `/suggestions/visits/`) — no endpoint.

*(Resolved: photos CRUD + upload (owner_slug/wiki_slug/wiki_name/dm_peer_slug/
dm_peer_name all confirmed present), voting (`{value: -1|0|1}`, explicit clear via 0),
media labels (bare name-string list, not label objects), memories journal
(`{entries, total}` envelope, with real `icon`/`subtitle`/`url` fields this app hadn't
modeled before), visit suggestions accept/dismiss, and API-credential support on the
media gate (`media:read` scope) are all live and confirmed correct.)*

## 8. Safety Check-ins [P1]

- **[P1] Owner-side chat has no REST endpoint at all** — only the tokenized
  contact-portal WebSocket exists (which the app already links out to). The owner's own
  side of that same conversation has nothing to call.
- **[P1] Partner accept/decline (the invitee's own side)** — the owner can invite/remove
  partners, but an invited partner has no endpoint to accept or decline.
- **[P1] Partner-side "mark {owner} safe" action** — no endpoint.
- **[P2] Live location fields** (`live_location_sharing_enabled`/`live_latitude`/
  `live_longitude`) — deliberately not exposed yet; this needs its own privacy-scoped
  design (a continuously-updating precise position stream is a different privacy
  proposition than the rest of this surface), not a quick field addition.
- **[P2] Field-locking isn't enforced at the API level yet** — `contacts_locked`/
  `notifications_locked` exist as read fields, but the update endpoint currently accepts
  edits regardless of check-in status.

*(Resolved: check-in CRUD (`grace_period_seconds`, confirmed — not minutes),
check-in/cancel, partners invite/remove (`{username}`, full check-in echoed), contacts
defaults GET/PUT, preferences GET/PATCH, and photos/maps attach-by-reference
(`{image_uuid}`/`{map_uuid}`, not raw upload/inline GeoJSON) are all live and confirmed
correct.)*

## 9. Social [P1/P2]

- **[P1] No unblock endpoint** — block exists (`POST /friends/{uuid}/block/`), nothing
  reverses it.
- **[P1] No avatar upload endpoint.**
- **[P1] Profile "private notes about a friend" is a different shape than the app wants,
  not just a different path.** The real `/profiles/{slug}/notes/` is a collection of
  independent freeform `{uuid, content, created, updated}` rows — there's no
  nickname/trust concept anywhere server-side. If the app's nickname+notes+trust design
  is still wanted, that needs new fields on the real model; this is a product
  conversation more than a build-it ask, flagging so it doesn't get lost.
- **[P2] `PATCH /profiles/{slug}/` doesn't accept `display_name`, `contact_methods`, or
  `social_links`** — only `bio`/`area`/`started_exploring` (+ the visibility fields).
  Please confirm whether that's intentional or an oversight.
- **[P2] Friend-mute's exact request body isn't confirmed** — `POST
  /friends/{uuid}/mute/` is real, but we assumed a bodyless toggle from the response
  shape (`{profile_uuid, is_muted}`) alone. Please confirm.

*(Resolved: friend request/accept/reject/ignore/block, friend-invites
(`POST /friend-invites/`, anti-enumeration confirmed byte-identical), account settings
(comprehensive — confirmed to cover every privacy/history/community field this app
wants), notifications + mark-read (`POST /notifications/{uuid}/ {action: "read"}`) +
unread-count, notification delivery preferences (`/notification-preferences/`, flat
object keyed by type name, confirmed exact 12-type/4-value-delivery/whatsapp+sms shape),
and push-device registration (`transport: "unifiedpush"|"fcm"`, confirmed) are all live
and confirmed correct. Note: friendship rows have no id of their own — every action
addresses the other profile's uuid directly.)*

## 10. Search, Enrichment, Misc [P1/P2]

Confirmed **still entirely absent** from `dashboard/external_api/` — nothing shipped
here this pass:

- **[P1] `GET /search/?q=`** — global natural-language search.
- **[P2] External-data enrichment panels as JSON** (weather, satellite/street-view,
  regional data) for a pin.
- **[P2] Full-account backup export/import** (`tools/export/*`, `tools/import/*`) — real,
  confirmed dashboard routes; no external-API mirror.
- **[P2] Undo** (`GET /undo/`, `POST /undo/{id}/restore/`).
- **[P2] `GET /site/config/`** — feature flags/quotas/tile-layer URLs.
- **[P2] AI chat assistant** (`/assistant/message/`, `/assistant/reset/`) — real,
  confirmed dashboard routes; no external-API mirror.

## 11. Operational & Account Settings [P1]

Confirmed **still entirely absent**:

- **[P1] Settings sync** (`GET/PATCH /settings/` mirroring the site's account
  preferences) — no such endpoint; see §9's confirmed comprehensive `/settings/` shape
  for what this domain already exposes, which is a *different* settings resource
  (privacy/style/map/markup/AI/community/wiki-sync toggles) than the app's own local
  mirror is asking to sync against. Worth clarifying which one is meant if this gets
  picked up.
- **[P2] Immich/Flickr/Google Photos account connections** — real, confirmed dashboard
  routes (`settings/immich/`, `settings/flickr/*`, `settings/google-photos/*`); no
  external-API mirror.
- Rate limits, CORS, pagination convention, API versioning discipline — unchanged asks
  from before this pass, not re-verified.

## 12. External-Data Panels (location intelligence + media gallery) [P2]

Confirmed **still entirely absent** as an external-API surface, though the underlying
`dashboard/plugins/` system (~20 provider plugins) is real and live internally. The ask
remains one generic `GET /pins/{slug}/panels/{key}/` (+ a `GET /pins/{slug}/panels/`
listing endpoint) rather than ~20 bespoke ones — see the plugin registry's uniform
`gate()`/`is_ready()`/`fetch()` interface. Also still needed: API-credential support on
the media gate for panel-sourced images (photos' own media gate now has this, per §7 —
same gap here).

## 13. SpotGuessr (game) [P2]

Confirmed **still entirely absent** from the external API — `controllers/spotguessr.py`
and its services are real, well-defined JSON endpoints, but session-authenticated only.
The app's solo-play mode assumes `POST /spotguessr/start/`,
`GET /spotguessr/session/<id>/round/`, `POST /spotguessr/session/<id>/round/<rid>/guess/`,
`GET /spotguessr/pins/`, `GET /spotguessr/`. The ask, if this becomes a priority: confirm
or add these on the external API surface with API-key auth. Multiplayer
(lobby/invite/join/WebSocket/chat) is unimplemented client-side and not being asked for.

## JSON conventions the app already assumes

- snake_case keys; ISO-8601 UTC datetimes (except a handful of confirmed bare-date
  fields — pin/wiki `date_*`, trip `start_date`/`end_date`, safety check-in
  `grace_period_seconds` is a duration in seconds not a date); decimal lat/lng as
  numbers; GeoJSON for polygons/linestrings; enum values matching the Django
  `TextChoices` values.
- Errors: `{"error": "..."}` with meaningful HTTP status; field errors are nested,
  `{"error": "Invalid request.", "fields": {"field": ["msg"]}}` on 400. (This spec
  previously described DRF's raw `{"detail": ...}` / top-level-field-keyed shapes;
  `external_api.errors.ErrorEnvelopeMixin` is now inherited by both package view
  bases, so `detail` never appears and field errors are never top-level. Every
  raised 404 renders as the constant `{"error": "Not found."}` regardless of the
  upstream message - deliberately, so a 404 body cannot be used to distinguish
  "does not exist" from "exists but is not yours".)
- List pagination is page-number style (`{results, next, previous, count}`) almost
  everywhere, except the pin/tombstone sync feeds (`{pins|tombstones, next_cursor,
  sync_watermark}`, cursor-based) and a few confirmed non-paginated envelopes (wiki
  gallery-less endpoints, safety contacts `{contacts, rejected}`, memories journal
  `{entries, total}`, visit suggestions `{suggestions}`, notification preferences a flat
  object keyed by type name rather than a list at all).
