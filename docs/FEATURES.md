# UrbanLens Features

A feature inventory of what UrbanLens currently supports, generated from a codebase audit
(2026-07-11, last verified/expanded 2026-07-18). This is a snapshot, not a promise — see `TODO.md` for what's planned or partially
built, and `docs/NOTES.md` for non-obvious behavior behind these features.

## Mapping & Pins

- Interactive Leaflet map with 9 configurable layers (Street, Terrain, Satellite, Weather, Dark,
  Borders, Places, Pins, Sub Pins), HTMX-driven panels, and a filter sidebar (labels, rating,
  visited status, date pinned, scores, saved filter configurations)
- **Pin** — a user's personal record for a place (custom name, private notes, icon, priority,
  status, last-visited date, marker coordinates), separate from the shared **Location** record
  it points to (canonical name, address, coordinates, Google CID). See `docs/NOTES.md` for why
  this split exists.
- Pin types: location, building, entrance, POI, danger, other
- Add pins by map click, coordinate entry, or place search/autocomplete; drag to reposition
- Pin list view alongside the map (particularly useful while searching/filtering); "Add these pins to a list" bulk action from the pin list panel adds all currently-visible/filtered pins to a trip or saved collection at once
- Bulk pin operations: multi-select, bulk edit, bulk merge, bulk delete (with undo)
- Per-pin alternate names (**aliases**) — private aliases on a Pin vs. shared aliases on a Wiki;
  names are unique per pin/wiki case-insensitively. Deleting an auto-added alias, link, label, or
  property owner is permanent - automatic sources (external name lookups, AI extraction,
  keyword/AI auto-tagging) won't silently recreate something you removed
- Private per-pin notes (`PinNote`), independent of public comments
- **Private per-pin Article** — Wikipedia-style long-form private notes per pin (sections, links, references) with full **revision history** (every saved version stored, restorable from the Edit History tab)
- Pin sharing — share a single pin with one friend, including re-share chains; every share
  records a provenance chain (`LocationExposure`) of how a location reached each user
- Import/export: Google Takeout (Saved Places, Location History, My Activity), GPX, GPX tracks,
  OSM XML, Shapefile, WKT/WKB, KML/KMZ; AI-assisted import from freeform documents/notes
- Data export/import of a user's full dataset, plus scheduled/on-demand backups

## Search & Navigation

- Logged-in home page (`/dashboard/home/`) — a customizable widget dashboard (stats, recent
  pins/photos/comments/maps/trips, upcoming trips, active safety check-ins, ...); users pick
  which widgets show and reorder them, saved per-profile
- **Global search** (navbar, Ctrl+K) across result types (pins, wikis, photos, trips,
  messages, …) with lightweight natural-language parsing ("photos from last summer",
  "pins in Cincinnati", "pins near me", "messages from Alice"), pg_trgm typo tolerance,
  and a plain-text fallback when no structured interpretation matches

## Lists & Saved Filters

- **Pin lists** — ordered, slug-addressed collections of pins with their own detail page
  (list-scoped map using the shared toolbar/layers, drag-to-reorder, bulk add from the current
  map filter); create a trip from a list, add a list's pins to an existing trip, or generate a
  markup map from one
- **Smart lists** — lists auto-populated from saved-filter criteria and resynced automatically
  as pins and labels change
- **Saved filters** — reusable filter configurations with full CRUD (managed alongside lists at
  `/lists/`), name suggestion, live match counts, and geographic include/exclude polygon
  regions selected via boundary search; usable from the map's filter sidebar and as smart-list
  criteria

## Locations & Community Wiki

- **Location** — shared, address-authoritative record for a physical place; coordinates are
  immutable after creation (mutable address/geocode metadata only)
- **Wiki** — opt-in, community-editable page for a Location: description, aliases, community
  danger/vulnerability/rating stat voting (`WikiStatVote`, fuzzed community counts for privacy),
  edit history with revert (`WikiEdit`)
- Place-name resolution across multiple sources (Google Places, OSM/Nominatim, NPS, Photon, EPA ECHO, **Azure Maps**, Wikipedia, OpenStreetMap former name) with agreement-based priority ordering and a **user-configurable drag-to-reorder name source priority list** in Settings → Privacy
- Boundary drawing — property/building polygons per pin, generated automatically from external
  building-footprint data where available, editable by the user
- Standalone reusable **MarkupMaps** with freehand drawing/annotation tools (point, line, arrow, text, box, circle, polygon), attachable to pins, wikis, safety check-ins, or kept independent; also embedded in the **safety check-in creation form** for drawing routes and destinations
- Detail pins — sub-markers placed inside a pin/wiki's bounding box for finer-grained mapping
  (rooms, entrances, hazards, etc.)

## External Data Enrichment (Pin Detail Page)

On-demand, cached lookups shown as panels on the pin detail page:

- **Wikipedia** — best-matching article
- **Wikimedia Commons**, **Smithsonian Open Access**, **Library of Congress** — archival photos/media
- **National Park Service** (USA) — nearby park info
- **LoopNet** (USA) — commercial real-estate listings
- **Property Records** (USA) — automated county parcel ownership/tax/sale-history lookup via free
  ArcGIS REST/Socrata county GIS endpoints, with US Census-based jurisdiction resolution; populates
  the wiki's Ownership and Sale History cards with `OFFICIAL`-sourced records in addition to a
  details card. Coverage depends on the county-by-county `PropertyJurisdiction` registry
  (site-admin) — see `docs/property-records-plan.md` and `docs/PROBLEMS.md` for the tiered
  fallback design and what's not automated yet (Tiers 2/3: vendor-platform and bespoke-scraper
  counties)
- **USGS Historical Topo Maps** (USA) — historical topographic maps
- **Nominatim/OpenStreetMap** — reverse geocoding and place metadata (two panels: Nominatim structured data and Photon nearest-feature lookup)
- **Regional Data** — US Census, Wildlife, Seismic, and EPA data loaded on demand per sub-tab
- **Building Characteristics** — structured property/building data (appears for commercial and historic properties)
- **News** — web news results scoped to the location (appears for notable locations)
- **OpenWeatherMap** — weather forecast; appears on Trip detail pages (keyed to activity location) and on the pin detail page when weather data is available
- Satellite imagery carousel: Google Maps, Esri (incl. Wayback historical imagery), NASA GIBS,
  Mapbox, Bing Maps, OpenAerialMap
- Street-view carousel: Google Street View, Mapillary, KartaView
- Web/image search panels (Google/Brave search, Google Image Search)
- Debug overlay (admin-only) to inspect raw external-API responses per panel

All external integrations are cached (DB-backed, per-Location) and rate-limited per service, with
usage tracked in `ApiCallLog`/`ApiRateLimit` and toggled at `/site-admin/api-limits/`.

Beyond on-demand fetches, an hourly **background enrichment** task drips high-value lookups
(official names, aliases, street addresses, building boundaries) into whatever rate-limit budget
is left over after real traffic, spread evenly so multi-day quotas can't be burned in one day.
Sources are plugin-contributable (`EnrichmentSource`) and admin-tunable (run window, reserve
buffer, per-run caps).

## Extensibility: Plugin System

Third-party integrations are packaged as **plugins** (`dashboard/plugins/builtin/`) — see
`docs/plugins.md` for the full contribution API. A plugin can add rate-limited services, pin-detail
panels, satellite/street-view providers, place-name providers, and lifecycle hooks. Plugins are
discoverable from bundled modules, an env-var module list, or pip entry points, and can be
enabled/disabled per-install or per-service without a restart. Inventory at `/site-admin/plugins/`.

## Photos & Memories

- Photo galleries on pins and wikis: drag-drop upload, reordering, lightbox, EXIF/GPS extraction,
  checksum-based duplicate detection
- Site-wide photo library (Memories → Photos) that matches unfiled photos (by GPS + timestamp) to
  existing pins and proposes **visit suggestions** for confirmation
- **Memories** page — aggregated timeline/map view of routes, trips, visits, and photos, including
  an "on this day" retrospective and a prompt to log visits for pins already marked visited; tabs
  for Timeline, Photos, Maps, Sharing, Journal, and Visits; date range filter with presets
  (Last 90 days / Last year / All time); "Import routes & history" for importing GPS tracks and
  location history (separate from the map's pin import flow)
- **Pin suggestions** — batch photo-location ingestion (a client-side local-folder scanner on
  the Tools page, or a full Immich library sweep) matches photo GPS against existing pins and
  clusters the rest into suggested new pins, reviewed on a multi-select map with bulk accept,
  pagination, and opt-in photo import
- Storage quota accounting per user (role-based), automatic downscaling/WebP conversion on upload

## Trips

- Multi-stop trip planning shared among friends: activities, scheduling, map view
- RSVP per member, per-activity thumbs up/down voting on proposed activities
- Trip comments with emoji reactions
- List and calendar views of trips, sortable
- Two-way Google Calendar sync — connect an account, import calendar events as trips
  (attendees become friend invites), export trip activities to Calendar
- Trip settings controlling member/organizer permissions

## Safety Check-ins

- "I didn't come home" style safety net: create a check-in with expected return time and
  emergency contacts (registered friends or external email contacts)
- Escalation on missed check-in: emails emergency contacts, optionally posts to the location's
  community wiki, notifies pin owners
- Public (tokenized, no-login) contact portal for emergency contacts to mark the user safe,
  view attached maps, and chat in real time
- Live two-way WebSocket chat between check-in owner and emergency contacts
- Reusable saved emergency contacts, per-contact opt-out, auto-delete retention policy

## Social Layer

- Friendships: request/accept/reject/ignore/remove/block/mute, invite by email
- Configurable friend-request visibility ("anyone", "friends of friends", "anything in common", etc.)
- Public/friends-scoped profile pages with visibility controls per field (9 controls, each with 7 granularity levels from "Anyone" to "No one"), "view my profile as..." preview mode
- **Identity masking in shared spaces** — a trip or group chat member whose `profile_visibility`
  doesn't permit another member to see them shows as an anonymous "Member" (name/avatar hidden,
  distinct color/number per hidden person so several aren't indistinguishable) in the member
  list, activity attribution, comments, and group messages; their content still shows. Adding
  someone unconnected to a trip/group chat sends both sides a soft "you might know each other"
  notification (gated on each person's "allow friend recommendations" setting) — never an
  automatic friend request or profile-view bypass
- **"Show Photos From" visibility** — photos from users outside your chosen tier are blurred rather than hidden
- Reviews (0–5 star rating, no text) and comments (with @mentions, emoji reactions, image
  attachments) on pins, wikis, and trips
- Private per-profile notes and trust ratings you keep about other users (not visible to them)
- Multiple verified email addresses per account, for easier friend discovery
- Social/community links on profile (site, Discord/Signal/etc.)

## Labels (Tags, Categories, Statuses, People)

A single unified `Label` model (with a `kind`) backs four distinct UI concepts:

- **Tags** — freeform labels on pins/wikis
- **Categories** — hierarchical classification of pins/wikis
- **Statuses** — workflow state labels
- **People labels** — private labels a user applies to other profiles

Shared features across all four: create/edit/delete, merge, hierarchical parent/child
relationships, bulk edit and bulk convert between kinds, per-user color/icon customization
(`LabelCustomization`) on top of shared global labels, drag-to-reorder priority, and a unified
"Organize" management page.

## Notifications

- In-app notification center (bell dropdown), mark read/unread, per-type delivery preferences
- Real-time push over WebSockets (`ws/notifications/`) with desktop `Notification` API support and
  a 60s polling fallback
- Outbound email notifications with per-role rate caps (hourly/daily/monthly) and safety controls
- **11-event × 4-channel notification matrix** (Settings → Account): each event type (new message, friend request, check-in alert, AI task completion, etc.) can be independently configured for in-app, email, WhatsApp, and SMS delivery. WhatsApp/SMS require a phone number on the profile. **Delivery caveat:** WhatsApp/SMS dispatch is currently implemented only for safety check-in alerts and new direct messages — the other event types' WhatsApp toggles are stored but not yet wired to a sender (see `docs/PROBLEMS.md`).
- Admin-only critical alerting via email + Gotify push (distinct from user-facing notifications)

## Custom Fields

User-defined private fields for **pins**, **photos**, **people**, and **maps**. Power-user feature for tracking non-standard attributes (e.g. access status, personal reference IDs, condition notes). Managed in Settings → Advanced.

## External Photo Integrations

- **Immich** — connect a self-hosted Immich instance (server URL + API key) to browse and import nearby photos linked to pins
- **Google Photos** — OAuth import from a connected Google Photos library

## Account & Auth

- Email/password signup with verification, plus Google and Discord OAuth (social-auth pipeline)
- Password reset (themed to match the app, not bare Django pages)
- **Passkeys** (Face ID, Windows Hello, security keys, Bitwarden-compatible) and **TOTP 2FA** (Google Authenticator, Authy, Bitwarden TOTP); backup codes available once passkey or TOTP is configured
- OAuth accounts can set a password separately to enable new-device encryption unlock without the recovery key
- Self-service account deletion (request with grace period, cancel)
- First-run setup wizard and a first-login onboarding tour with feature opt-outs; contextual
  in-product help tooltips on first visit to key sections (e.g. trip permissions, itinerary),
  with "Don't show again" opt-out per tooltip
- Login lockout after repeated failed attempts

## Undo / Data Safety

- Generic, cache-backed undo framework: deleting a pin, wiki, safety check-in, or trip stages the
  action for a limited window before it's finalized
- Settings → Undo History page to review and restore recently undo-able actions

## Site Administration

- `/site-admin/` panel: user management, site-wide settings, usage stats (KPIs, system, API),
  subscription role management, per-service API rate-limit toggles, plugin inventory, UI component
  showcase, dev toolbar (theme/map-dark-mode toggles, session reset)
- Data export/import tooling and on-demand/scheduled database backups
- Subscription roles grant feature flags (`SiteFeature`) per user; pending grants can attach to an
  email invite for users who haven't joined yet

## AI Integration

- Pluggable AI provider gateway (OpenAI, Cloudflare, Hugging Face)
- AI-assisted import: extract pins from freeform documents/notes
- AI-assisted label styling: suggest colors/icons for auto-created labels
- Keyword-based and AI-assisted auto-tagging of pins/wikis
- **AI link extraction** — a per-link sparkle button (on the pin's Links card and inside
  external-data panels such as web search, Wikipedia, LoopNet, and news results) has AI read the
  linked page and extract allowlisted structured fields (date built, date abandoned, owner
  name/company, sale date/price, aliases) into the pin; admin-settable per-user daily limit, a
  review page (`/ai/extractions/`) for results that couldn't be applied automatically, and a
  completion notification
- **Local keyword tagging** — entirely local (no AI or network call), keyword-match auto-categorize / auto-tag / auto-status on pin save; master toggle + per-type sub-toggles in Settings → Connections

## REST API

DRF `ModelViewSet`s under `/dashboard/rest/`, session-authenticated:

- `pins` — full CRUD on the requesting user's pins
- `reviews` — full CRUD plus a `create_or_update` action for star ratings
- `profiles` — profile data (read-only)
- Notification access via a matching viewset

## Direct Messaging

- End-to-end encrypted 1:1 direct messages and named group chats
- **Group-chat scope (deliberate, as of 2026-07-18):** group chats support text (plaintext or
  E2EE), pin sharing (one provenance-tracked PinShare per member), rename, creator-managed
  membership, per-member mute, and unread tracking. They intentionally do *not* yet have 1:1
  parity for: reactions, image attachments, replies/quotes, map attachments, coordinate/address
  detection, disappearing messages, typing indicators, read receipts, or delete-for-self (only
  the sender's delete-for-everyone exists). A group whose creator leaves becomes permanently
  unmanaged (no ownership transfer). Extending any of these is a product decision, not a bug fix.
- Rich compose toolbar: image attachment, share location/map, share pin, @mention, emoji. The
  map composer dialog has two tabs - draw a new map, or choose one of your existing maps (search
  by title) - both attach the same way
- Fallback (initial-letter) avatars use a deterministic per-person color that's guaranteed
  distinct from everyone else shown in the same list (e.g. a group chat's member dialog), so two
  people without photos never look identical there
- Read receipts, online status indicator, typing indicator (visibility of each configurable per user)
- Per-message emoji reactions
- Message search — within a single conversation or across all of them, with jump-to-message
  scroll and highlight
- Coordinates and street addresses pasted in chat are auto-detected and offered a one-click
  "Add to my map"
- Pin sharing into group chats, with per-member accept/reject
- **Disappearing messages** — configurable per-account expiry (never / on read / 1 day / 30 days / 90 days / 1 year)
- E2E encryption key management in Settings → Messages: view or reset recovery key; old messages
  encrypted under a rotated key are shown inline as "Unable to decrypt on this device" with a lock icon
- **Friend recommendations** opt-in toggle (Settings → Messages)

## Real-time (WebSockets)

- `ws/notifications/` — live notification push per logged-in user
- `ws/messages/` — direct-message delivery, typing indicators, read/open tracking, and
  reaction updates for DMs and group chats (with an HTTP fallback for sending)
- `ws/safety/checkin/<uuid>/chat/` and `ws/safety/contact/<token>/chat/` — safety check-in chat,
  shared between the check-in owner and emergency contacts
