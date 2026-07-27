The following list was provided by an agent working on the mobile app, providing input on what changes UrbanLens needs to make in order to allow the mobile app to work correctly, with full parity with the existing web app features.

Don't take their suggestions as gospel. Our codebase here is more mature than theirs, and is the basis of what we're trying to do when there is a conflict. However, when the requests made are consistent with our goals and can be implemented safely, securely, maintainably, extensibly, and performantly, we should implement them. 

If any of these are not achievable within those constraints, create a note of that in docs/notes/mobile_app_notes.md

# Required UrbanLens Server Changes

The app makes **no modifications** to the UrbanLens repository. This document is the
complete list of server-side work the app depends on, in priority order. It follows the
server's own `docs/external_app_api_plan.md` and `docs/designs/mobile-app-stack-r2.md`:
a dedicated `dashboard/external_api/` app mounted at `/dashboard/api/external/v1/`,
scoped credentials (PAT `ulk_…` keys and OAuth2+PKCE tokens), thin serializers calling
the existing service layer, per-credential throttling, and usage logging.

As of server v0.6.0 the pins domain is substantially live — delta-sync feed,
tombstones, idempotent create, OAuth2 provider, push-device registration, an OpenAPI
schema at `…/v1/schema/` (unauthenticated; validate this contract against it in CI),
and a session-authenticated media gate. Items below are marked **✅** where shipped.

Legend: **[P0]** app is unusable against a real server without it · **[P1]** major
feature gap · **[P2]** parity polish.

## 1. Authentication — OAuth2 + PKCE [server ✅ v0.6.0; app client ✅ 2026-07-23]

- **[P1] First-party client registration.** The app now ships a complete
  authorization-code + PKCE (S256) client and signs in via the system browser.
  Register a **public** OAuth2 application (no secret, PKCE required) with:
  - `client_id`: **`urbanlens-app`** (or tell us the assigned id — it's a
    single constant in the app, `AppConfig.oauthClientId`)
  - redirect URI: **`urbanlens://oauth/callback`**
  - grant type: authorization-code; token endpoint auth: none (public client)
  The app requests scopes `profile:read pins:read pins:write push:manage`,
  exchanges at `POST /oauth/token/` (form-encoded), and handles the rotating
  refresh tokens (single-flight refresh, replacement persisted immediately,
  one forced-refresh retry on 401). Until the client is registered, the
  browser flow fails at the authorize page and users fall back to pasted keys.
- **[P1] Wider scopes as domains ship.** Beyond the current four, matching the plan
  doc's domain-scope guidance: `lists:read/write`, `wiki:read/write`,
  `trips:read/write`, `photos:read/write`, `visits:read/write`, `social:read/write`,
  `safety:read/write`, `notifications:read`, `search:read`, `messages:read/write`
  (transport of already-E2EE payloads only — plaintext DM bodies should *never* be
  issuable to external credentials).
- **[P1] E2EE login params.** The client still fetches
  `/dashboard/e2ee/login-params/` for the password-derived unwrap key; confirm it
  stays public JSON alongside the OAuth2 flow.
- **[P2] Token introspection** (`GET /auth/session/`-equivalent: scopes, expiry) for
  the settings screen.

## 2. Pins, Locations, Map data [partially ✅ v0.6.0]


- **[P1] Create-payload gaps.** `POST /pins/` accepts only
  name/latitude/longitude/address/icon/color/uuid — the app's capture flow also
  sets `description` and `pin_type` and must currently defer them to a follow-up
  PATCH (which needs the detail endpoint below).
- **[P1] Label `kind` in the sync payload.** `tags` entries carry
  `{id, name, color, icon}` but not `kind` (tag/category/status), so the app cannot
  distinguish them without re-deriving from `status`/`categories` strings.
- **[P0] `GET /pins/{slug}/`** — full detail: description, dates
  (built/abandoned/last_active), security fields, notes, aliases, links, custom fields,
  boundary GeoJSON, cover photo URL, wiki slug (if discovered), counts.
- **[P0] `PATCH /pins/{slug}/` / `DELETE`** — extend the existing internal
  `PinViewSet` semantics (lat/lng moves relink the Location; delete stages undo +
  writes the tombstone) to the external app with `pins:write`.
  **[2026-07-26]** the app's `ApiPinRepository.updatePin` now also sends a
  `parent_id` field (a pin uuid, or `null` to detach) on every PATCH, to
  support PIN.SHELL-03 (detach) and MAP.POPUP-04 ("Promote all children",
  each a bulk re-parent). This field name is **assumed, not confirmed** — the
  PATCH endpoint isn't live to check against, and the *sync feed's* read-side
  key for the same relationship is `parent_uuid` (normalized to `parent_id`
  client-side, see `normalizePinJson`), so the PATCH side may turn out to use
  `parent_uuid`, `parent`, or something else entirely. Please confirm the
  actual field name when this endpoint ships, and whether it enforces the
  site's "no second root pin at the same Location" invariant
  (`PinDetachChildView`, `UrbanLens/.../controllers/pin_edit.py:473-498`) or
  expects the app to guard against that client-side.
- **[P1] Sub-resources:** notes, aliases, links, visits
  (`GET/POST /pins/{slug}/visits/`), articles + revisions, detail pins — CRUD each.
  **[2026-07-25]** aliases also need a "use this name" action —
  `POST /pins/{slug}/aliases/{aliasId}/use/` returning the renamed pin (the old
  name becomes a new alias, never lost outright), assumed to mirror the wiki
  version (`POST /wikis/{slug}/aliases/{aliasId}/use/`, `aliases_panel.html`'s
  shared "use this name" button — same partial serves both Pins and Wikis).
- **[P1] `GET /locations/search/?q=`** — place autocomplete (wraps the existing
  `map/search/…` provider chain).
- **[P2] Bulk ops:** `POST /pins/bulk/` (edit/delete/merge/label) matching the site's
  multi-select toolbar.

## 3. Lists, saved filters, labels [P1]

- `GET/POST/PATCH/DELETE /lists/`, `/lists/{slug}/items/` (ordered add/remove/reorder),
  smart-list resync trigger.
- `GET/POST/PATCH/DELETE /saved-filters/` (criteria JSON is already a portable format —
  document it in the serializer).
- `GET/POST/PATCH/DELETE /labels/` incl. kind (tag/category/status/people), hierarchy,
  merge; per-user customization of globals.
- **[P2] Label reorder.** `LabelReorderView`
  (`dashboard/controllers/labels.py`) persists drag-and-drop order for tags/
  categories/statuses (`Label.order`) — no equivalent exists on `/labels/`
  yet. **[2026-07-26]** The app now ships drag-and-drop reorder UI
  (LABEL.CRUD-04) against an **assumed, unconfirmed** REST analogue:
  `POST /labels/{kind}/reorder/ {tag_ids|category_ids|status_ids: […]}`
  (id-key mirrors the dashboard view's own per-kind naming), returning
  `{"ok": true}` like the dashboard view — the client re-fetches the kind's
  rows afterward rather than trusting a response body. Please confirm the
  actual path/shape (or add the endpoint) before this ships against a real
  server; `ApiLabelRepository.reorderLabels` is CONTRACT-commented at the
  call site.
- **[P2] Label bulk delete.** `LabelBulkDeleteView`
  (`dashboard/controllers/labels.py`) bulk-deletes profile-owned labels by
  id, silently skipping protected status labels rather than erroring — also
  no equivalent on `/labels/`. **[2026-07-26]** The app now ships a
  long-press multi-select → bulk-delete UI (LABEL.BULK-01) against an
  **assumed, unconfirmed** endpoint: `POST /labels/bulk-delete/
  {ids: […]}`. Unlike the dashboard's per-kind URL, this is id-scoped with
  no kind in the path or payload (mirroring how `mergeLabels` above is also
  id-scoped, not kind-scoped) — the assumption is that the server can
  resolve each id's own kind/ownership/protection without a kind hint. If
  the real endpoint turns out to be kind-prefixed like reorder's, both the
  interface method and its one caller (`organize_screen.dart`'s bulk-delete
  flow) would need a `kind` parameter added.
- **[P2] Priority (cross-kind label) reorder.** `OrganizePriorityListView` /
  `OrganizePrioritySaveView` (`dashboard/controllers/organize.py`) read/persist
  one combined display order across tag/category/status labels together
  (same `Label.order` field as the per-kind `LabelReorderView` above, just
  not kind-scoped — first item in the submitted list gets the highest
  `order = total - i`) — no equivalent on `/labels/` or anywhere else in the
  REST v1 surface. **[2026-07-26]** The app now ships a "Priority" tab
  (ORG.PRIORITY-01/02) against an **assumed, unconfirmed** call: the client
  posts directly to the dashboard's own confirmed route,
  `POST /organize/priority/save/ {"items": [{"id": 1}, {"id": 2}, …]}`
  (id order = desired display order), since no REST v1 analogue is known to
  exist — unlike `reorderLabels`/`bulkDeleteLabels` above, this isn't a
  guessed-at REST-style path, it's the real dashboard URL, but it's still
  unconfirmed whether this client's base URL/auth can actually reach a
  dashboard route rather than only `/api/v1/...`-style endpoints. The
  response shape is also unconfirmed (assumed no usable body, same as the
  other two), so the client re-fetches every label afterward rather than
  trusting one. Please confirm reachability (or add a REST v1 equivalent)
  before this ships against a real server; `ApiLabelRepository.
  reorderPriority` is CONTRACT-commented at the call site. The GET side
  (`OrganizePriorityListView`) was **not** used — the app already holds the
  full label list via the existing `listLabels()`/`organizeLabelsProvider`,
  and filters out People and Media client-side, mirroring the server's
  `_NON_PRIORITY_KINDS` gate. **[2026-07-26]** Now that `LabelKind.media`
  exists app-side (LABEL.MEMBERSHIP-03), this is a real filter rather than
  a no-op — see the media-label-membership entry in §7 for where the fifth
  kind came from.

## 4. Wikis & community [P1]

- `GET /wikis/{location_slug}/` — **must route through the existing
  `resolve_visible_wiki()`** so discovery-gating and the identical-404 oracle guarantee
  hold for API clients too.
- Wiki edit (`PATCH`), edit history + revert, stat votes
  (`PUT /wikis/{slug}/votes/{field}/`), aliases, links, gallery, article + revisions.
- Comments & reviews: `GET/POST /pins/{slug}/comments/`, reactions, star reviews
  (server already has `ReviewViewSet.create_or_update` — expose externally).
- **[2026-07-25] Community pin-count/first-pinned fields.** `wiki.html`'s
  "Community" card (distinct from "Community Ratings") shows a privacy-fuzzed
  pinner count (`services/community_counts.py`'s `approximate_pin_count` —
  never reveals an exact count under 3, fuzzes ±2 above that, cached 24h so
  refreshing can't average out the noise) and a "First pinned {month year}"
  tile. The wiki JSON needs two read-only fields the client renders as-is, no
  fuzz math app-side: `pin_count_low` (bool) + `pin_count_approx` (int, null
  when low) and `first_pinned` (date, null if unknown). App added `Wiki.
  pinCountLow`/`pinCountApprox`/`firstPinned` this session; no endpoint
  confirmed yet, `api_wiki_repository.dart` does no normalization so these
  parse automatically once the server sends them.
- **[2026-07-25] Article save-conflict (`base_revision_id`).** The site's own
  `ArticleSaveView.post()` (`dashboard/controllers/article.py:199-238`) takes
  a `base_revision_id` field (the latest `ArticleRevision.id` when the editor
  was opened) and rejects with a 409 + toast if the article's current latest
  revision has since moved on. That view is HTMX/session-only though, not the
  external API — `api_wiki_repository.dart`'s `saveArticle` now sends the same
  `base_revision_id` on the assumed `PUT /wikis/{slug}/article/` body as a
  CONTRACT guess that the external API mirrors this semantics, and maps a 409
  to the new `ApiErrorKind.conflict`. Please confirm the external article-save
  endpoint accepts `base_revision_id` and returns 409 (ideally with the same
  "This article changed while you were editing…" detail) on a stale id.
- **[2026-07-26] Wiki dates + security indicators (WIKI.EDIT-01/03).** The
  "Suggest edits" dialog now also edits `date_abandoned`/`date_last_active`
  and the 8 security-indicator fields (`fences`, `alarms`, `cameras`,
  `security`, `signs`, `vps`, `plywood`, `locked`) — confirmed against the
  server's own `Wiki` model (`models/wiki/model.py`: `date_abandoned`/
  `date_last_active` are plain `DateField`s; the 8 fields come from
  `abstract.SecurityModel`, each a `CharField` defaulting to
  `SecurityLevel.UNKNOWN`) and against the site's own internal edit endpoint
  (`LocationWikiEditView`, `POST /location/<slug>/wiki/edit/`,
  `_WIKI_EDITABLE_FIELDS`/`_WIKI_SECURITY_FIELDS` in
  `controllers/location_wiki.py`), which takes all 10 fields as flat
  top-level `field: value` pairs (dates as bare `"YYYY-MM-DD"`, security as
  the raw `SecurityLevel` string). **That view is session/HTMX-only, not the
  external API** the app's `PATCH /wikis/{slug}/` hits. CONTRACT: rather than
  matching the site's flat shape, `api_wiki_repository.dart` sends
  `date_abandoned`/`date_last_active` as ISO-8601 datetime strings (via
  `DateTime.toIso8601String()` — same as this app's existing
  `WikiPropertySale.date` for the analogous `sale_date` `DateField`) and a
  nested `security: {field: value}` object, mirroring `ApiPinRepository.
  updatePin`'s existing (also-unconfirmed) shape for Pin's identical 8-field
  set, on the assumption the external serializer treats Pin and Wiki
  consistently even though the two known *internal*-site shapes (`pin_edit.py`
  vs. `location_wiki.py`) already differ from each other. Please confirm
  whether the external API instead wants the site's own flat-field shape, and
  whether date fields should be truncated to `YYYY-MM-DD` rather than a full
  datetime.
- **[2026-07-26] Scoped out: markup-shape security-indicator side effect.**
  WIKI.EDIT-03 also describes chips being settable "as a side-effect of
  drawing a markup shape with a security indicator" — confirmed server-side
  in `controllers/markup.py`'s `_apply_security_indicator`/
  `_INDICATOR_TO_FIELD` (a markup item can carry a `SecurityIndicatorType`
  that upgrades the matching Pin/Wiki security field from unknown/no to
  "some"). The app's own markup model/editor (`lib/features/markup/`,
  `MarkupMap`/`PinMarkup`-equivalent) has no security-indicator concept at
  all yet — confirmed via grep, zero hits — and WIKI.MARKUP-01's own
  PAGE_FEATURES.md note already says as much ("security-indicator aren't
  modeled for pin markup either yet"). Out of scope for this change; belongs
  with whatever session builds out WIKI.MARKUP-01 for both Pin and Wiki
  markup together, not invented ad hoc here.

## 5. Trips [P1]

- Trips CRUD, activities (+ voting), membership/RSVP, comments (+ reactions),
  trip map data (`GET /trips/{slug}/map/` exists internally as JSON already).
- Google Calendar sync remains server-side; app only needs
  `POST /trips/{slug}/calendar-sync/` toggle + status field.
- **[2026-07-25] Self-leave.** `DELETE /trips/{slug}/leave/` — confirmed server
  route (`TripLeaveView.delete()`, `controllers/trip.py`): deletes the caller's
  own `TripMembership` row, 400s if the caller is the trip's creator (they must
  delete the trip instead). App added `TripRepository.leaveTrip` against this
  exact path.
- **[2026-07-25] Member management.** Confirmed server routes
  (`controllers/trip.py`): `DELETE /trips/{slug}/members/{profile_id}/remove/`
  (`TripMemberRemoveView`, creator-only, 400s if the target is the creator) and
  `POST /trips/{slug}/members/{profile_id}/organizer/`
  (`TripMemberOrganizerView`, creator-only, 400s if the target is the creator).
  The site keys both by integer `profile_id`; the app assumes its own API keys
  by slug instead (`{memberSlug}`), matching every other member-targeting
  endpoint already assumed elsewhere in this app. App added
  `TripRepository.removeMember`/`toggleOrganizer` against these paths.
- **[2026-07-26] Create-trip: blank name + description (TRIP.CREATE-01/02).**
  `POST /trips/` — the create-trip dialog no longer blocks on an empty name;
  the app assumes the server auto-generates a themed name (site shows a
  12-flavor rotating placeholder) when `name` arrives blank, and
  `ApiTripRepository.createTrip` now sends a blank `name` through as-is
  rather than fabricating one client-side. Also added `description` to the
  same POST body, assumed to use the same `'description'` key as the existing
  `PATCH /trips/{slug}/` trip-edit body. Neither assumption is confirmed
  against the server's actual `TripCreateView`/serializer — flagging for
  verification.
- **[2026-07-26] Add member (TRIP.MEMBERS.ADD).** Confirmed server route
  (`controllers/trip.py:1506-1552`): `POST /trips/{slug}/members/`
  `{username}` — `TripMembersView.post()`. Creator (or whoever
  `trip.allow_add_members` permits — the app gates on creator-or-organizer,
  its closest client-side equivalent) only. Looks up the target by exact,
  case-insensitive `username` match (no fuzzy search); 404s with `No user
  found with username "{username}".` if none. Rejects with 403 and the
  deliberately vague `This user isn't accepting invitations from you.` if
  the caller and target are blocked in either direction (anti-enumeration —
  never discloses who blocked whom). 400s with `This trip is full ({max}
  members maximum).` at `SiteSettings.max_trip_members` — a server-side
  setting the app can't query, so `DemoTripRepository` stands in with a
  fixed `_maxTripMembers = 20` for demo mode only; the real cap is whatever
  that site setting is configured to. Idempotent (`get_or_create`): adding
  an already-member username succeeds without creating a duplicate row and
  without counting against the cap. On success, creates the membership with
  an "invited" status — as of 2026-07-26 the app has a real
  `TripMember.status`/`TripMemberStatus` field for this (see TRIP.DETAIL-02
  below), replacing the earlier `TripRsvp.pending`-as-stand-in approximation
  this paragraph previously described. Also fires a notification + a
  "suggest mutual connections" side effect server-side; both are
  server-only, nothing for the app to do. App added `TripRepository.
  addMember` against this exact path/body shape. TRIP.CREATE-04
  (invite-friends checklist at trip-creation time) was blocked on this same
  capability existing — it's now unblocked, but TRIP.CREATE-04 itself
  remains a separate, not-yet-implemented follow-up.
- **[2026-07-26] Join a trip (TRIP.DETAIL-02).** `TripMembership` has a real
  `status` field (`STATUS_INVITED`/`STATUS_JOINED`, default `joined` —
  `dashboard/models/trips/model.py:307-337`) distinct from `rsvp`: it gates
  whether a member can contribute (add/edit activities, comment, vote, add
  other members) at all. `TripMembershipJoinView` sets the caller's own
  status to `joined`. CONTRACT (unconfirmed): assuming `POST
  /trips/{slug}/join/`, mirroring the confirmed `DELETE /trips/{slug}/leave/`
  shape it sits alongside; `ApiTripRepository.joinTrip` echoes the updated
  trip if the response body has one, else refetches. Declining an
  invitation reuses the existing `leaveTrip`/`DELETE /trips/{slug}/leave/`
  outright rather than a separate endpoint — confirmed by the server's own
  docstring ("reuses TripLeaveView instead, since a not-yet-joined member
  has no contributions to lose by leaving"). App added
  `TripRepository.joinTrip` and a `TripMember.status`/`TripMemberStatus`
  model field, plus an `_InviteBanner` (Join/Decline) on
  `trip_detail_screen.dart` shown only to an invited-not-joined viewer.
- **[2026-07-26] Drag-to-reposition an activity marker (TRIP.DETAIL.MAP-05).**
  `TripActivity` has real `lat_override`/`lng_override` `FloatField`s
  (`dashboard/models/trips/model.py:224-226`, comment: "Map position
  override — set when user drags the marker; does NOT modify the underlying
  Pin/Location") — confirmed, not guessed. `activity_coords()`
  (`dashboard/services/trip_legs.py:39-51`) resolves effective coordinates
  with priority override → pin → location. The dashboard already has a
  working JSON endpoint for this exact drag: `POST
  /trips/{slug}/activities/{id}/position/` `{lat, lng}`
  (`TripActivityPositionView.post()`, `controllers/trip.py:1985-2029`) sets
  both override fields and returns `{lat, lng}`; the site's own map
  (`detail.html`) calls it via `fetch()` on marker `dragend`, gated by a
  `PIN_DRAG_CONFIRM_ZOOM = 14` pre-drag confirm at high zoom. CONTRACT
  (unconfirmed): the app's external API (a separate surface from the
  dashboard's own routes, per every other CONTRACT note in this section) may
  or may not expose the identical path — `ApiTripRepository.
  updateActivityPosition` assumes it does (same path/body shape) and
  refetches the trip afterward since the response doesn't echo the full
  aggregate. Please confirm the external API has (or add) an equivalent
  `lat`/`lng`-only position-update endpoint that never touches
  `TripActivity.pin`/`location`. App added
  `TripRepository.updateActivityPosition`/`TripController.
  updateActivityPosition`, and `trip_detail_screen.dart`'s `_ActivityMap`
  drags the selected marker with the same zoom-gated confirm the site uses.

## 6. Messaging & E2EE [P1]

The E2EE JSON endpoints under `/dashboard/e2ee/…` (enroll, keys, conversation-key,
group-key, rewrap, login-params) are exactly what the app needs but are
**session-cookie-gated**. Change: accept `ApiKeyAuthentication` (scope
`messages:write`) on these views, or mirror them under `/api/external/v1/e2ee/…`.

> **Contract note (server change, 2026-07-23):** `E2EEGroupKeyView` now keys group
> members by an opaque per-(group, member) HMAC token — `GET` returns
> `[{id, public_key}]` and `POST wrapped` must be keyed by those same tokens.
> Member slugs are no longer exposed (identity-masking privacy fix). The app's
> future wrap/rewrap implementation must be written against the token shape;
> slug-keyed payloads get a 409.

> **Contract note (server change, found 2026-07-24):** `E2EEEnrollView`,
> `E2EERewrapView`, and `E2EEResetView` now require a `current_password` field
> in the JSON body for password accounts (`_require_current_password_proof` in
> `controllers/e2ee.py`) — a 403 on mismatch, closing a hijacked-session
> key-replacement hole. Currently moot for the app (none of these three flows
> are built yet — see `docs/PAGE_FEATURES.md` E2EE.ENROLL-06/RECOVERY-03), but
> whoever builds recovery-key view/regen or key reset needs its own password
> re-entry step before calling these, distinct from the existing unlock
> dialog's password field.

- `GET /messages/conversations/` (threads + unread), `GET /messages/{peer_slug}/`
  (paginated history incl. `ciphertext/nonce/key_version` fields),
  HTTP send fallback, reactions, read receipts, disappearing-message settings.
- **Send payload share fields:** the app's composer can attach share references
  alongside (or instead of) a body — `shared_pin_id` (pin slug),
  `shared_trip_slug`, `shared_profile_slug`, `markup_map_id`. The server should
  resolve them with the sender's access rights and record share provenance
  exactly as the site's share flow does (recipient access follows provenance
  rules, never the app).
- Group chats: list/create/rename/membership, messages, per-member key envelopes.

> **Contract note (app assumption, 2026-07-26):** `Conversation.creatorSlug`
> (JSON key `creator_slug`) is a new field the app added to model
> `GroupChat.creator` (`dashboard/models/group_chats/model.py`) — nullable,
> matching the server's `on_delete=SET_NULL`. **Unconfirmed against a real
> `GET /messages/conversations/` response** — assumed present on group rows
> (null on DMs) and omitted/null when the creator's profile was deleted. The
> app uses it purely to pre-gate the add/remove-member UI (MSG.GROUP-02:
> only the creator manages membership; a null creator means *nobody* can,
> not "any member" as a fallback); the real 403 from
> `GroupAddMembersView`/`GroupRemoveMemberView` remains the actual
> enforcement (`PermissionError` in `services/group_chats.py`, per
> `GroupChat.is_manager`). Whoever wires the real endpoint should confirm
> the field name/shape and adjust `Conversation.fromJson` if it differs.
- **[P0-within-messaging] WebSocket auth:** `AuthMiddlewareStack` only reads session
  cookies. Add an API-key WS auth middleware (e.g. `?key=ulk_…` query param or
  `Sec-WebSocket-Protocol: bearer.ulk_…`) for `ws/messages/`, `ws/notifications/`, and
  the owner-side safety chat. The token-authenticated contact chat
  (`ws/safety/contact/{token}/chat/`) already works for the contact portal.

## 7. Photos & memories [P1]

- `POST /photos/` multipart upload (server already does EXIF/GPS extraction, checksum
  dedupe, WebP downscale, quota — reuse the service layer), attach to
  pin/wiki/visit/checkin.
- `GET /photos/` with `pin`, `unfiled`, `date_range` filters; gallery reorder; delete.
- Visit suggestions: `GET /suggestions/visits/`, accept/dismiss. Pin suggestions
  likewise for the photo-scan flow.
- Memories feeds: timeline, on-this-day, journal — read-only JSON. The app now
  renders a Journal tab against `GET /memories/journal/` returning
  `[{id, kind: visit|rating|comment, title, body, pin_slug, rating,
  occurred_at}]` (mirrors `services/memories/journal.py`).
- **Media serving [partially ✅ v0.6.0]:** `MediaGateView` now authenticates and
  authorizes every `/media/` request (closing the open-nginx hole), but it is
  session-only (`LoginRequiredMixin`). Remaining ask: accept API credentials
  (PAT/OAuth2 bearer) on the media gate — until then the app cannot fetch photos in
  api mode.
- **[2026-07-26] Wiki photo voting (WIKI.MEDIA-04).** Thumbs up/down on a
  wiki-shared photo, net score re-sorts the grid, never gates/hides the
  photo, no durable Image row or quota spend. No endpoint confirmed —
  `api_media_repository.dart`'s `votePhoto` CONTRACT-assumes
  `POST /photos/{id}/vote/ {up: bool}` as a *toggle*: posting the same
  direction twice withdraws the vote, mirroring this app's existing
  re-click-to-clear vote convention (`WikiRepository.vote`/`clearVote`) but
  as one call instead of two, since the swing math (first vote / switch /
  withdraw) has to happen server-side — only the server knows the caller's
  currently-stored vote. Response is the updated Photo with new
  `my_vote`/`vote_score`. Please confirm the real path/method and whether
  it truly toggles or needs an explicit clear like the wiki stat votes.
- **[2026-07-26] Media-label image membership (LABEL.MEMBERSHIP-03).** The
  server's `LabelImageMembershipView` (`dashboard/controllers/labels.py`,
  confirmed) lets an image's own uploader add/remove `kind='media'` labels
  on it — `get_object_or_404(Image, uuid=…, profile__user=request.user)`
  (404s for anyone else's image, not just a permission error), and the
  label lookup is filtered to `Label.objects.visible_to(profile).media()`
  (a non-media label id 404s too). That view is HTMX/session-only
  (`POST /labels/image/{image_uuid}/ {label_id, action: add|remove}`), not
  a REST v1 endpoint. The app added `LabelKind.media` (the fifth
  `Label.kind`, confirmed against `dashboard/models/labels/meta.py`'s
  `KIND_CHOICES`) and a chip+picker panel (`MediaLabelPanel`, in the shared
  full-screen photo viewer since that's reused across the pin/wiki/memories
  galleries alike) gated to the photo's own owner. Two CONTRACT gaps this
  introduces, both unconfirmed:
  - **`Photo.ownerSlug`.** No documented photo payload carries who owns an
    image yet (§7's `/photos/` shape is itself still aspirational) —
    assuming a `owner_slug` field alongside it, the same style as
    `Comment.authorSlug`/`TripComment.authorSlug`. Without this the panel
    has no way to gate itself to the uploader, so please add it (or point
    at whatever field already carries this) before this ships against a
    real server.
  - **`setImageLabels` endpoint.** `ApiLabelRepository.setImageLabels`
    assumes `PUT /photos/{id}/labels/ {label_ids: […]}` returning the
    updated photo — a bulk-set REST analogue of the HTMX add/remove-one
    view above, mirroring `setPinLabels`'s own bulk-set shape
    (`PUT /pins/{slug}/labels/`) rather than the server's real per-item
    POST, the same simplification `setPinLabels` already makes for pins.
  Simplification: the client's bulk "set the whole list" call doesn't
  distinguish add from remove the way the real dashboard view's
  `{label_id, action}` payload does — same trade-off `setPinLabels` already
  made, kept for UI consistency (the panel's chip list always submits its
  full desired set, one call per add/remove tap).
- **[2026-07-26] Photo attachment points panel (PROFILE.ATTACH-01).** The
  server's `PhotoAttachmentPointsView` (`controllers/userprofile.py`,
  confirmed) and `services.profile_photos.attachment_points_for_image`
  describe, for one of the *owner's own* photo-strip photos, where it's
  attached — a wiki share (`Image.wiki`) and/or the DM it was sent through
  (`Image.direct_message`) — as a lightbox side panel; it 204s (panel
  absent, no header) when neither is set. Confirmed the two aren't mutually
  exclusive: the service computes each independently and appends both to
  the same `points` list, so a single photo can be shared to a wiki *and*
  sent as a DM attachment at once — the app's demo seed (`photo-21` in
  `demo_world.dart`) now models that combined case explicitly. Confirmed
  this is not people/face-tagging — no such feature exists anywhere on the
  real site; the panel only ever describes where the photo *itself* is
  reachable from, never who appears in it. The real view's HTMX response
  bakes ready-made `{icon, label, url}` dicts (`"Wiki: {name}"` /
  `"Sent to {username}"`) rather than raw slugs/names, so the app added
  four fields to `Photo` mirroring that baked shape instead of adding a
  wiki/profile lookup to the panel:
  - **`wikiName`** — the linked wiki's display name, alongside the existing
    `wikiSlug`.
  - **`dmPeerSlug`** — profile slug of the DM's other party, matching
    `Conversation.id`'s "peer profile slug for DMs" shape
    (`lib/domain/models/messaging.dart`) so the panel's jump link routes
    straight to `/messages/{slug}` with no extra lookup.
  - **`dmPeerName`** — that peer's display name, for the "Sent to {name}"
    label.
  None of these four are confirmed on any documented `/photos/` payload
  yet (§7's shape is itself still aspirational) — CONTRACT: assuming
  `wiki_name`/`dm_peer_slug`/`dm_peer_name` alongside `wiki_slug` on
  whatever shape `GET /photos/` ends up returning, the same style as
  `owner_slug` above. Please confirm the real field names, or point at
  wherever this information actually lives once `/photos/` is real.
  App-side: `PhotoAttachmentPointsPanel`
  (`lib/features/memories/photo_attachment_points_panel.dart`), added to
  the same shared full-screen photo viewer as `MediaLabelPanel` (reused
  across pin/wiki/memories galleries) and gated to the photo's own owner
  with the exact same `photo.ownerSlug == mySlug` check. Scope cut: the
  profile page's own photo-strip screen (PROFILE.VIEW-03, "only 'not fully
  private' photos") is a separate, still-unimplemented bullet, so this
  panel has no profile-page entry point of its own yet — it's reachable
  today wherever the shared photo viewer already opens on the owner's own
  photos (e.g. the Memories gallery).

## 8. Safety check-ins [P1]

- CRUD check-ins, contacts (reusable defaults), mark-safe, gallery, attach markup map.
- The tokenized contact portal + its WS chat are already session-free; the app links
  out to them (no change).
- **[2026-07-25] Partners.** Server added `SafetyCheckinPartner` (invite-by-username,
  no friendship required, must accept before gaining visibility) the same day. The
  app assumes `POST /safety/checkins/{id}/partners/ {username}` and
  `DELETE /safety/checkins/{id}/partners/{partnerId}/`, both returning the updated
  check-in with its `partners` array — no confirmed endpoint shape yet
  (`api_safety_repository.dart`'s existing CONTRACT-assumption pattern). Only the
  owner-side invite/remove is implemented app-side so far; NOT yet covered:
  the invitee's accept/decline (site: a "Pending partner invites" card on Safety
  home, `SafetyCheckinPartnerInviteAcceptView`/`...DeclineView`), the partner's
  read-only detail view, and the partner-side "Mark {owner} safe" action
  (`SafetyCheckinPartnerMarkSafeView`) — all three need the app to model check-ins
  owned by other profiles, not just the signed-in user's own, a bigger structural
  change than the invite/remove CRUD added this pass. Live location sharing
  (`live_location_sharing_enabled`/`live_latitude`/`live_longitude`, same day's
  commit) also has zero app support yet — see PAGE_FEATURES.md's SAFE.LIVELOC.
- **[2026-07-26] In-place autosave (SAFE.DETAIL-02).** Added
  `SafetyRepository.updateCheckin(SafetyCheckin)`, wired to the detail screen's new
  `planDetails`/`contactMessage` autosaving fields (`checkin_detail_screen.dart`'s
  `_AutosaveField`). The app assumes `PATCH /safety/checkins/{id}/` with
  `{title, plan_details, contact_message, destination_latitude,
  destination_longitude, notify_community_wiki}` — no confirmed endpoint shape yet
  (same CONTRACT-assumption pattern as Partners above). This is a guess at what a
  clean JSON API for this repository's existing `/safety/checkins/{id}/` mount
  *would* look like; the real server-side edit endpoint that actually exists today
  is different in kind, not just path: `SafetyCheckinDetailView.post`
  (`dashboard/controllers/safety.py:775`, confirmed by reading it directly) is a
  form-encoded dashboard view keyed by the check-in's slug, not a JSON REST
  endpoint, and it returns `{ok, warnings, contacts_html, title}` for an XHR
  autosave rather than the full updated check-in. That view's real field-locking
  semantics (worth preserving if/when a real API endpoint is confirmed): title is
  frozen once `checkin.contacts_locked`; `contact_message`/contact list/
  `notify_community_wiki` are frozen once `checkin.notifications_locked`; plan/
  destination stay editable always, but editing either once contacts are locked
  re-notifies them (debounced via `notify_contacts_of_update`'s cooldown). NONE of
  that locking is modeled app-side yet — this pass (SAFE.DETAIL-02) intentionally
  ships full-time editability with no locking at all; SAFE.DETAIL-03
  (PAGE_FEATURES.md) is the tracked follow-up for the locking/re-notify behavior.
  `contacts`/`partners` are deliberately left out of `updateCheckin`'s request body
  — no in-place "edit contacts" UI exists yet to justify guessing that shape.

## 9. Social [P1]

- Friends: list, request/accept/reject/block/mute/remove, invite-by-email.
  **[2026-07-26] wire-value mismatch (pre-existing, not introduced by this
  note's date).** `FriendshipStatus` (`lib/domain/models/enums.dart`)
  serializes plain lowercase enum names (`'accepted'`, `'blocked'`, …) for
  every member. The server's real `FriendshipStatus` is a `TextChoices` with
  capitalized values (`"Accepted"`, `"Declined"`, `"Removed"`, `"Blocked"`,
  `"Ignored"`, …, `dashboard/models/friendship/meta.py`) — this violates this
  project's own stated convention (see CLAUDE.md: "Enum wire values match
  Django `TextChoices`"). Every one of the app's existing 4 values
  (`requested`/`pending`/`accepted`/`blocked`) already had this gap before
  the 2026-07-26 FRIEND.LIST-07 pass added 3 more (`declined`/`removed`/
  `ignored`) using the same (mismatched) convention for consistency, rather
  than fixing only the new values and leaving old/new inconsistent with each
  other. Net effect against a real server: every `Friendship.status` would
  currently deserialize to `unknown` via the `unknownEnumValue` fallback,
  since none of the app's lowercase values match.
  **[2026-07-26, fixed later the same day]** `@JsonValue('Requested')` etc.
  added per member — confirmed this enum really is the outlier (not a
  systemic issue) by cross-checking `PinType.LOCATION_MARKER = "location",
  "Location"` in the sibling repo, whose wire value genuinely is lowercase
  like its Dart name, unlike `FriendshipStatus`'s capitalized ones. Demo
  mode was unaffected either way (it never round-trips `Friendship` through
  JSON) — this only matters once the API repository path is exercised
  against a live server, which still hasn't happened, so the fix is
  unverified against a real response but matches the `TextChoices`
  declaration exactly.
  **[2026-07-25]** the app assumes `POST /friends/invite/` `{email, message}`
  for invite-by-email, mirroring the confirmed server route
  (`FriendshipView.invite_by_email`, `controllers/friendship.py:603-663`) —
  no endpoint confirmed for the app's own API surface yet. Response must stay
  byte-identical regardless of match/no-match/privacy-reject (anti-
  enumeration); the app never branches on it, matching that constraint.
- Profiles: `GET /profiles/{slug}/` honoring the 9-field visibility matrix server-side;
  own-profile PATCH; private notes/nicknames/trust CRUD — the app assumes
  `GET/PUT /profiles/{slug}/private-note/` `{nickname, notes, trust}` (owner-scoped,
  404 until first written).
- Avatar upload: the app assumes `POST /profiles/me/avatar/` multipart (`file`),
  mirroring the photo-upload shape in §7, returning the updated profile with its new
  `avatar_url`. No endpoint documented yet.
- Notifications: `GET /notifications/` + mark-read (+ the WS above);
  `GET /notifications/unread-count/` exists internally — expose with key auth.
- **[2026-07-26] `safety_ci_due`/`visit_suggested` notification-dropdown row
  shapes (NOTIF.DROPDOWN-02).** Confirmed both against
  `dashboard/templates/dashboard/partials/notifications/notification_item.html`
  and their originating services directly.
  - `safety_ci_due`: real rows always render two actions regardless of the
    check-in's own status (no `is_actionable`-style gate for this type) —
    "Check in" (`href="{{ n.url }}"`, the same `SafetyCheckin.checkin_by`
    reminder URL `send_checkin_reminder` sets, `services/safety.py:1308-1318`
    — i.e. the exact SAFE.DETAIL-01 self-check-in action, driven client-side
    by `SafetyRepository.markSafe`) and "View check-in"
    (`href="{{ n.url|cut:'checkin/' }}"`, the detail page). The server's
    `url` is always set for this type (`/safety/<checkin_slug>/checkin/`,
    confirmed — never blank), so the app parses the check-in id straight out
    of it (`safetyCheckinIdFromUrl` in
    `lib/features/notifications/notifications_screen.dart`) instead of
    adding a new `AppNotification` field — no CONTRACT needed here, the
    external API's existing `url` is trusted to keep this shape.
  - `visit_suggested`: real rows branch on `VisitSuggestion.offers_merge`
    (`dashboard/models/visit_suggestions/model.py`) — true when the
    recipient already has a same-day visit logged at this place
    (`existing_visit_id is not None`) — showing 3 buttons ("Add to my
    visit" → `action=add_participants` → `merge_visit_suggestion`, "Log
    separately" → `action=new_entry` → `accept_visit_suggestion`, "Reject" →
    `action=reject` → `reject_visit_suggestion`, all confirmed in
    `dashboard/controllers/visit_suggestions.py`'s
    `VisitSuggestionRespondView`); otherwise the standard 2-button
    accept/reject shape (`action=accept`/`reject`). **Unlike
    `safety_ci_due`, this type's `NotificationLog` row is created with no
    `url=` kwarg at all** (`dashboard/services/visits.py`'s
    `create_visit_suggestion`, confirmed by reading it directly) — a real
    `visit_suggested` notification has a blank url, so its suggestion id
    can't be recovered the same way. CONTRACT: added `AppNotification.
    visitSuggestionId`/`.offersMerge` as new fields (no confirmed external-
    API serializer shape includes them yet) and
    `NotificationRepository.respondToVisitSuggestion(suggestionId, action)`,
    assumed at `POST /visit-suggestions/{id}/respond/ {action: "accept"|
    "reject"|"add_participants"|"new_entry"}` mirroring
    `VisitSuggestionRespondView` 1:1. Please confirm the real notification
    serializer shape and the endpoint path/method once an external API
    surface for visit suggestions exists.
  - **Scope cuts:** the server's "visit logging disabled" no-op/toast case
    (`visit_logging_allowed`, gating `accept_visit_suggestion`/
    `merge_visit_suggestion`) isn't modeled — the app has no client-side
    visit-history-tracking setting to check against yet, so a response
    always "succeeds" from the app's perspective. The app also doesn't
    persist a `VisitSuggestion` row anywhere (demo or api); once a response
    call succeeds, `NotificationsController.resolveVisitSuggestion` just
    clears the notification's local `visitSuggestionId`, hiding its action
    row — a local stand-in for the server re-checking
    `VisitSuggestion.is_actionable`, not a faithful mirror of it (e.g. it
    won't reflect a suggestion resolved from a *different* client/session).
    The demo world doesn't create a `PinVisit`/merge participants either —
    `DemoNotificationRepository.respondToVisitSuggestion` is an intentional
    no-op stub, since modeling the downstream visit-log side effects is a
    separate, much larger feature than the notification row itself. Note
    this app also already has an unrelated `MediaRepository.
    listSuggestions`/`.resolveSuggestion` ("Suggestion"/MEM.VISITS, backing
    the Memories Suggestions tab) — confirmed by reading both the app and
    server that this is a **different, wholly CONTRACT-assumed** concept
    (`/suggestions/{id}/accept|dismiss/`, no real server endpoint at all
    yet, see §7) unrelated to the real `VisitSuggestion` model backing this
    notification type; they were not merged/reused, deliberately.
- **[P1, app-side ✅ 2026-07-26; server endpoint still needed] Notification
  delivery preferences (NOTIF.PREFS-01..03).** `NotificationPreferencesView`
  (`dashboard/controllers/notifications.py`) lets a user set, per notification
  type (12 types — `_PREF_FIELDS`, confirmed by reading the model directly:
  friend_request, friend_accepted, message, comment_reply, comment_liked,
  pin_shared, visit_suggested, added_to_trip, trip_updated, wiki_updated,
  wiki_safety_checkin, safety_checkin_partner_invite — the last two default
  to `both` instead of `site`), which channels deliver it. This is
  dashboard-only (built from raw `request.POST`, no Django Form and no DRF
  endpoint) — needs a `GET/PATCH /notifications/preferences/` endpoint before
  the app can round-trip against a real server.
  **Confirmed grid-to-enum mapping** (read `notification_preferences.html` +
  `NotificationPreferencesView.post` directly): the site shows 5 checkboxes
  per row (None/Notification/Email/WhatsApp/SMS) but only 4 are real inputs.
  "None" has no `name` attribute and is never submitted — it's a decorative,
  client-JS-only checkbox auto-checked when neither Notification nor Email is
  checked. The real `{type}__site`/`{type}__email` checkboxes combine into
  the server's single `DeliveryPreference` CharField (`none`/`site`/`email`/
  `both`). WhatsApp/SMS are NOT part of that enum — `{type}_whatsapp`/
  `{type}_sms` are two fully independent `BooleanField`s, each gated (client
  AND server-side, `NotificationPreferencesView.post`'s `can_whatsapp`/
  `can_sms`) on `Profile.whatsapp_number`/`.phone_number` being non-empty.
  **App-side (2026-07-26):** `lib/domain/models/notification_prefs.dart`
  (`NotificationPrefs`/`NotificationTypePref`/`DeliveryMode`),
  `NotificationRepository.getPreferences`/`.updatePreferences`
  (`lib/domain/repositories/repositories.dart`), demo (`DemoWorld.
  notificationPrefs`) and api implementations, and
  `lib/features/settings/notification_prefs_screen.dart` (linked from
  Settings > Notifications). **CONTRACT** (no endpoint confirmed on the
  external API yet — `api_notification_repository.dart`): assumed
  `GET/PATCH /notifications/preferences/` request/response body
  `{"types": [{"type": "friend_request", "delivery": "site", "whatsapp":
  false, "sms": false}, …]}`, one entry per type above, PATCH always
  rewriting the full list (mirrors the site's own always-full-POST
  semantics). The app's own gate for WhatsApp/SMS reuses the existing
  `Profile.contactMethods['whatsapp']`/`['phone']` entries (already editable
  via the profile editor's `_contactPlatforms` list, `profile_screen.dart`)
  rather than adding new `Profile` fields — no server contract change needed
  for the gate itself, only for the preferences endpoint above.
  The dashboard-only note this replaces (2026-07-25) reasoned a client-only,
  locally-stored stand-in would be actively misleading since real push
  dispatch reads the server's actual preference model — still true, which is
  why this repository always round-trips through `getPreferences`/
  `updatePreferences` rather than caching a local copy across sessions; demo
  mode's `DemoWorld.notificationPrefs` is the seeded in-memory equivalent of
  that same round trip, not a persistent local override.
  The template's separate "browser desktop-notification permission block"
  (`notification_preferences.html`) is a pure web-`Notification.
  requestPermission()` concern with no Android equivalent (NOTIF.PREFS-04,
  N/A) — not an independent app gap.
- Push **[registration ✅ v0.6.0; dispatcher ✅ 2026-07-23]**: `POST /push-devices/`
  (`transport` — UnifiedPush default — `address`, `name`; idempotent on address,
  scope `push:manage`) and `DELETE /push-devices/{uuid}/` are live, and
  `services/push.py` now dispatches on notification save (UnifiedPush endpoints
  validated against SSRF; https-only, no private hosts). Registered devices receive
  real deliveries. Remaining: an FCM transport for the Play flavor (FCM rows are
  accepted but deliberately not dispatched).

## 10. Search, enrichment, misc [P2]

- `GET /search/?q=` — global search (the site's natural-language search service).
- Enrichment panels as JSON for a pin: weather (+golden hour), Wikipedia match,
  satellite/street-view carousel frame URLs, regional data. Most internal providers
  already return JSON dicts — wrap them read-only.
- Import/export: `POST /imports/` (GPX/KML/Takeout/… multipart, async job + status
  URL), `POST /exports/` (GeoJSON/KML/GPX/CSV selection or list) → job → download.
- Undo: `GET /undo/`, `POST /undo/{id}/restore/`.
- `GET /site/config/` — feature flags (`SiteFeature`), quotas, tile-layer URLs, so the
  app can hide disabled features per install.

## 11. Operational & account settings

- **[P1] Settings sync.** The app mirrors the site's account preferences
  (privacy visibilities, history toggles, presence/retention, community
  flags) field-for-field against the server's `Profile` model and stores
  them locally. Ask: `GET/PATCH /settings/` (or include on `whoami`) with
  exactly those field names/TextChoices values so the app's stored values
  sync verbatim.


- **Rate limits:** the flat `120/hour` per key (`external_api/throttling.py`) is far too
  low for an interactive client syncing pins; suggest scope-tiered rates (e.g. reads
  1000/hr, writes 300/hr) still per-key.
- **CORS/hosts:** none needed for native clients, but desktop/web debug builds benefit
  from `CORS_ALLOWED_ORIGINS` including `http://localhost:*` in dev settings.
- **API versioning discipline** per the plan doc: `/v1/` is a frozen contract; additive
  changes only.
- **Pagination convention:** all list endpoints DRF cursor or page-number paginated —
  the app assumes `{results, next, previous, count}` page-number style.

## JSON conventions the app already assumes

- snake_case keys; ISO-8601 UTC datetimes; decimal lat/lng as numbers; GeoJSON for
  polygons/linestrings; enum values matching the Django `TextChoices` values
  (e.g. `pin_type: "building"`, `rsvp: "yes"`).
- Errors: `{"detail": "..."}` with meaningful HTTP status; field errors
  `{"field": ["msg"]}` on 400.