# Mobile App Notes

Notes back to the mobile app team on items from `docs/notes/mobile_app_requirements.md`
that are infeasible as requested, deliberately deferred, or need a contract clarification,
found while building out the pins/lists/wikis/trips/messaging/photos/safety/social external
API domains (server v0.6.0+, branch `feature/external-api-mobile-v2`).

This is a first pass covering concrete findings surfaced during that build, not an
exhaustive line-by-line reconciliation of every `CONTRACT`-flagged assumption in the
requirements doc - many of those (guessed field names, guessed endpoint shapes) can only
be confirmed by reading each domain's actual serializer/urls once this branch ships, which
is a follow-up task, not something re-derived here from memory.

## §1 Authentication - scope grant policy clarification

Requirement #1 asks for "wider scopes as domains ship" (`lists:*`, `wiki:*`, `trips:*`,
`photos:*`, `visits:*`, `social:*`, `safety:*`, `notifications:read`, `search:read`,
`messages:*`). **All of these scopes now exist** in `ApiKeyScope` and are enforced
per-endpoint - but `_default_api_key_scopes()` (`dashboard/models/account/model.py`) was
**deliberately not widened** to grant them automatically. This is a security decision, not
an oversight: silently expanding every already-issued PAT-style key's grant would hand an
integration reach its owner never consented to.

- **OAuth2+PKCE tokens** (the app's primary flow, per §1) already work today: the
  authorize screen's consent step lets the client request exactly the scopes it needs from
  the full vocabulary, so this is not a blocker for the app as built.
- **PAT-style keys** (`ulk_...`) still get only the original four
  (`profile:read`, `pins:read`, `pins:write`, `push:manage`) on creation, with no scope
  picker UI yet to request more. If the app (or its users) need PAT-based access to the
  newer domains, that scope-picker UI is real, not-yet-built follow-up work - flag as a
  separate ask if it's needed, rather than assuming the wider scopes are reachable via PATs
  today.
- `messages:*` is further restricted: it is **unreachable by any PAT-style key**, even one
  that somehow carries it (`OAUTH2_ONLY_SCOPES`, enforced in
  `external_api/permissions.py`) - by design, since PATs have no consent-screen step to
  show the user what's being requested. Encrypted-DM transport is OAuth2-only.

## §6 Messaging - WebSocket auth has no per-scope check (deferred)

Requirement #6 calls out `[P0-within-messaging] WebSocket auth` as needed, and it's now
live in the sense that `ApiKeyAuthMiddleware` (`websocket_auth.py`, wired into
`UrbanLens/asgi.py`) accepts a valid credential on `ws/messages/`, `ws/notifications/`,
etc. **However**, unlike the HTTP messaging endpoints (which correctly enforce
`messages:read`/`messages:write`), the WebSocket layer does not check the connection's
scopes at all - any valid, unrevoked credential can open `ws/messages/` regardless of what
scopes it holds. Full detail and the intended fix shape are in `docs/PROBLEMS.md` under
"Messaging / external API". **Not a blocker for initial integration** (the socket still
requires a real, unrevoked credential), but a scoped-down credential should not be assumed
to be denied live message/notification delivery until this is fixed.

## §4/§6 Markup-map share attachments bypass share provenance (deferred, pre-existing)

Attaching a `MarkupMap` to a direct message (or a wiki) does not record a
`LocationExposure`, unlike attaching a *pin* which correctly stamps the provenance chain.
This is not a regression introduced by the external API - the web composer has always
behaved this way, and the new API endpoints match existing behavior rather than diverging
from it. Not fixed in this pass since it needs a decision about whether a hand-drawn
annotation with no linked pin should count. See `docs/PROBLEMS.md` for the fix shape.

## §4 Wiki edit: internal endpoint stays lenient, external is strict

The external API's wiki-edit endpoint (`PATCH /wikis/{slug}/`) rejects an invalid
`security` value or malformed date with a hard 400, per requirement §4's expectation of a
real JSON API. The **internal** HTMX wiki-edit view was left on its pre-existing lenient
behavior (silently drops the bad field, returns `{"ok": true}`) rather than being migrated
to match - that's a separate, UI-shaped follow-up (needs field-level error rendering in
the About card), tracked in `docs/PROBLEMS.md`, and does not affect the external API
contract the app depends on.

## §2 `resolve_place` toggle bug is internal-only, not an external API gap

Not a mobile-facing item: `MapController.resolve_place` (the internal map's own endpoint)
has a bug where it doesn't honor `Profile.external_apis_enabled`, unlike its sibling
autocomplete endpoint. The **external** `GET /locations/resolve/` added for the app
correctly enforces this toggle already. Logged in `docs/PROBLEMS.md` for the internal-side
fix; no action needed on the app side.

## §4 Pins - creating a child (detail) pin near its parent now works via `parent_id`

`POST /pins/` accepts an optional `parent_id` (uuid of one of the caller's own pins).
Without it, coordinate resolution uses a 50m fuzzy-dedup radius (matching the map UI's
top-level "Add pin" flow) so repeat GPS drops at the same real place consolidate onto one
Location instead of spawning duplicates. That's correct for top-level pins, but it means a
genuine child pin - e.g. a building's north entrance, ~10m from its own main pin - would
previously get rejected outright with "You already have a pin at this location" before a
client could ever reach a reparent step, since there was no way to create a pin and mark it
as a child in the same call. `parent_id` fixes this: when given, Location resolution
switches to an exact-coordinate match (mirroring the map UI's own detail-pin flow in
`controllers/detail_pins.py`), and the pin is created with that parent already set - no
separate PATCH round-trip needed. The create response now also includes `parent_uuid`
(null for a top-level pin). `parent_id` must belong to one of the caller's own pins, or the
call is rejected with 400.

## Everything else in the requirements doc

The bulk of `mobile_app_requirements.md` (pins sub-resources, lists/saved-filters/labels,
trips CRUD + activities + comments, safety check-ins + partners, photos/memories,
social/friends/notifications) shipped as scoped endpoints on this branch. The numerous
`CONTRACT (unconfirmed)` notes scattered through that document - guessed field names like
`owner_slug`/`wiki_name`/`creator_slug`, guessed endpoint shapes for label reorder/bulk-
delete, photo voting, notification preferences, etc. - were **not individually
reconciled against the final implementation** as part of this merge pass. Recommend the
app team (or a follow-up session with both codebases open) diff each `CONTRACT` note
against the shipped OpenAPI schema at `/dashboard/api/external/v1/schema/` once this
branch is deployed, rather than trusting the assumed shapes silently matched.
