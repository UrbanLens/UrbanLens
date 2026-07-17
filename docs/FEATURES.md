# UrbanLens Features

A feature inventory of what UrbanLens currently supports, generated from a codebase audit
(2026-07-11). This is a snapshot, not a promise — see `TODO.md` for what's planned or partially
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
- Per-pin alternate names (**aliases**) — private aliases on a Pin vs. shared aliases on a Wiki
- Private per-pin notes (`PinNote`), independent of public comments
- Pin sharing — share a single pin with one friend, including re-share chains
- Import/export: Google Takeout (Saved Places, Location History, My Activity), GPX, GPX tracks,
  OSM XML, Shapefile, WKT/WKB, KML/KMZ; AI-assisted import from freeform documents/notes
- Data export/import of a user's full dataset, plus scheduled/on-demand backups

## Locations & Community Wiki

- **Location** — shared, address-authoritative record for a physical place; coordinates are
  immutable after creation (mutable address/geocode metadata only)
- **Wiki** — opt-in, community-editable page for a Location: description, aliases, community
  danger/vulnerability/rating stat voting (`WikiStatVote`, fuzzed community counts for privacy),
  edit history with revert (`WikiEdit`)
- Place-name resolution across multiple sources (Google Places, OSM/Nominatim, NPS, etc.) with
  agreement-based and admin-configurable priority ordering
- Boundary drawing — property/building polygons per pin, generated automatically from external
  building-footprint data where available, editable by the user
- Standalone reusable **MarkupMaps** with freehand drawing/annotation tools, attachable to pins,
  wikis, safety check-ins, or kept independent
- Detail pins — sub-markers placed inside a pin/wiki's bounding box for finer-grained mapping
  (rooms, entrances, hazards, etc.)

## External Data Enrichment (Pin Detail Page)

On-demand, cached lookups shown as panels on the pin detail page:

- **Wikipedia** — best-matching article
- **Wikimedia Commons**, **Smithsonian Open Access**, **Library of Congress** — archival photos/media
- **National Park Service** (USA) — nearby park info
- **LoopNet** (USA) — commercial real-estate listings
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
- Public/friends-scoped profile pages with visibility controls per field, "view my profile as..."
  preview mode
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
- Admin-only critical alerting via email + Gotify push (distinct from user-facing notifications)

## Account & Auth

- Email/password signup with verification, plus Google and Discord OAuth (social-auth pipeline)
- Password reset (themed to match the app, not bare Django pages)
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

## REST API

DRF `ModelViewSet`s under `/dashboard/rest/`, session-authenticated:

- `pins` — full CRUD on the requesting user's pins
- `reviews` — full CRUD plus a `create_or_update` action for star ratings
- `profiles` — profile data (read-only)
- Notification access via a matching viewset

## Real-time (WebSockets)

- `ws/notifications/` — live notification push per logged-in user
- `ws/safety/checkin/<uuid>/chat/` and `ws/safety/contact/<token>/chat/` — safety check-in chat,
  shared between the check-in owner and emergency contacts
