# UrbanLens Features

A feature inventory of what UrbanLens currently supports, generated from a codebase audit
(2026-07-11, last verified/expanded 2026-07-29). This is a snapshot, not a promise — see `TODO.md` for what's planned or partially
built, and `docs/NOTES.md` for non-obvious behavior behind these features.

## Mapping & Pins

- Interactive Leaflet map with 9 configurable layers (Street, Terrain, Satellite, Weather, Dark,
  Borders, Places, Pins, Child pins), HTMX-driven panels, and a filter sidebar (labels, rating,
  visited status, date pinned, scores, saved filter configurations)
- **Pin** — a user's personal record for a place (custom name, private notes, icon, priority,
  status, last-visited date, marker coordinates), separate from the shared **Location** record
  it points to (canonical name, address, coordinates, Google CID). See `docs/NOTES.md` for why
  this split exists.
- Pin types: location, parcel, building, entrance, POI, danger, other
- **Place** — one row per real-world parcel or building, and the unit everything shared hangs off:
  official geometry, the community wiki, boundary votes, and access. A coordinate resolves onto the
  most specific place containing it, so two people pinning opposite ends of one property share its
  page, its community, and its "places in common" entry without either coordinate being discarded.
  Buildings sit `PART_OF` their parcel; a split campus or a multi-parcel site sits above its parts
  via `MEMBER_OF`. See `docs/NOTES.md` and `docs/designs/place-consolidation.md`.
- **Parcel vs. building scope** — on a property holding several buildings, a marker commits to
  describing either the *grounds* or one structure. A parcel-scoped marker suppresses its
  building-level cards (CRIS Building USN Point, Building Attributes, Building Characteristics) in
  favour of a "Buildings on this Property" list, and draws only the parcel; a building-scoped
  marker draws only its own footprint, and its wiki is created with that footprint as its boundary.
  On an ordinary single-building property neither distinction exists, so markers stay neutral and
  both outlines are drawn. Scope is derived from the place and applies to *every* user's marker on
  it; an explicitly chosen type always wins. A badge in the page header names the scope whenever it
  isn't the neutral default. See `docs/NOTES.md`.
- **"Organize this property?"** — one suggestion, shown once the first time you open a pin's detail
  page, covering both halves of the same question: create a sub pin per building here (named and
  numbered from REData's county GIS + NY SHPO CRIS, or OpenStreetMap, and mirrored as child wikis
  when the place already has a community wiki), and nest any of your existing *top-level* pins that
  stand inside the property boundary — useful for maps built before child pins existed. Nesting only
  re-parents; nothing is merged, renamed, or deleted. Three answers: yes, no (permanent for that
  pin, even if new buildings turn up later), or don't show again (Settings → Map → Pin Organization
  Suggestions). Buildings you've already pinned are detected by their real footprint polygon, not a
  fixed radius, so a pin at the far end of a long hall still counts as covering it
- **Notes (pin comments) are never hidden by nesting** — the pin detail page's "show sub pin
  details" toggle (`?children=1`) aggregates a child pin's private notes into its parent's Notes
  tab too, each labelled with a link back to the sub pin it was written on, alongside the map,
  photo gallery, and visit history the toggle already covered
- **Manual pin ↔ wiki sync** — from the detail-pins multi-select toolbar, "Send to wiki" creates a
  matching child wiki for the selected sub pins, skipping ones the wiki already has; "Share with a
  friend" shares just the selected sub pins, not the pin's whole hierarchy. A "pull from wiki"
  button creates a personal sub pin for anything the community wiki already documents that you
  haven't pinned yourself. Neither direction ever creates the wiki itself - only its child wikis.
  Two building-typed markers are matched by REData's real building footprint when the parcel's
  buildings are known, not just proximity - a building pin shared from one end of a long hall and
  the receiving side's own pin at the other end still dedupe correctly, since both fall inside the
  same footprint even though they're farther apart than the fallback proximity radius covers.
  Non-building markers (entrances, hazards, POIs) are always proximity-matched
- **Community wikis nest themselves automatically** — when two independently-created wikis turn out
  to describe a place and something inside it (a building's wiki inside a campus's), the inner one
  becomes a child of the outer with no confirmation needed - re-parenting only, nothing else moves.
  Nesting follows place lineage, so it agrees with access by construction. See `docs/NOTES.md`.
- **One wiki per place** — creating a wiki for a coordinate that already has one, however far apart
  the two coordinates are on the same property, returns the existing page instead of a second one.
  A viewer who has earned the page reaches it from their own location's URL.
- Add pins by map click, coordinate entry, or place search/autocomplete; drag to reposition
- Pin list view alongside the map (particularly useful while searching/filtering); "Add these pins to a list" bulk action from the pin list panel adds all currently-visible/filtered pins to a trip or saved collection at once
- Bulk pin operations: multi-select, bulk edit (description, rating, labels, parent pin), bulk merge, bulk delete (with undo)
- Per-pin alternate names (**aliases**) — private aliases on a Pin vs. shared aliases on a Wiki;
  names are unique per pin/wiki case-insensitively. Deleting an auto-added alias, link, label, or
  property owner is permanent - automatic sources (external name lookups, AI extraction,
  keyword/AI auto-tagging) won't silently recreate something you removed
- Private per-pin notes (`PinNote`), independent of public comments
- **Articles** — Wikipedia-style long-form write-ups (sections, links, references) with full
  **revision history** (every saved version stored, restorable from the Edit History tab); private
  per-pin, or shared/community-editable per-wiki. Edited via a WYSIWYG canvas (click-to-format,
  no Markdown syntax required) with a Markdown "Source" mode for power users/footnotes - saved as
  plain Markdown either way
- Pin sharing — share a single pin with one friend, including re-share chains; every share
  records a provenance chain (`LocationExposure`) of how a location reached each user
- Import: Google Takeout (Saved Places, Location History, My Activity), GPX, GPX tracks, OSM XML,
  Shapefile, WKT/WKB, KML/KMZ; AI-assisted import from freeform documents/notes
- Targeted export of a pin selection (main map's multi-select toolbar) or a whole saved list
  (a list's "more actions" menu) as GeoJSON, KML, GPX, or CSV
- Data export/import of a user's full dataset, plus scheduled/on-demand backups

## Public Locations

A small, highly selective set of locations can be voted **public** by the users who already have
them pinned, and public locations are then suggested to every account (opt-out). The point is to
give a new user a populated map without exposing anything vulnerable — the eligibility rules are
the safety mechanism, so they run entirely server-side (`services/pins/public_pins.py`) and users
never see the rule engine, only vote buttons on a place that already qualifies.

- Voting is **anonymous in the UI**: a voter sees only their own choice, and no running tally is
  shown before an outcome is settled
- Candidates cycle `OPEN`/`SUSPENDED` as eligibility comes and goes; `PASSED`/`REJECTED` are
  terminal
- `evaluate_public_pin_candidates` runs hourly to re-run eligibility, settle open votes, and fan
  out suggestions — idempotent at any frequency

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
- **Wiki Media gallery** — the pin detail page's combined Media section, mirrored on the wiki
  (`controllers/wiki_media.py`): the same external providers (Wikimedia, Smithsonian, Library of
  Congress, Internet Archive, Web Images (SearXNG), Yelp, Google Images/Maps, LoopNet, CRIS, …)
  appear automatically
  from the shared per-Location cache, alongside a "Photos" tab of images intentionally shared to
  the wiki (`Image.wiki`) and a "Manage" tab for uploads. Thumbs-up/down are **community votes**
  (net score up − down, highest ranked first); because relevance is stored per-Location
  (`MediaRelevance`), a relevance mark made on any user's pin detail page already counts here
- **REData photo relevance scoring** — every new photo (upload, Google Places business photo
  backfill, or Media-gallery item materialized via "mark relevant"/"send to wiki") is submitted to
  REData's photo-scoring service with whatever signal is available (capture/location coordinates,
  capture date, uploader/photographer, wiki abandonment date); REData returns a calibrated
  confidence ("is this really a photo of this place") cached on the `Image` row
  (`services.photos.redata_relevance`). Relevant/not-relevant votes on a materialized photo are
  forwarded too, as REData's training signal - never as a scoring input. The pin detail page's own-
  photos preview and the wiki's Photos tab order by this confidence (vote score first on the wiki,
  confidence breaking ties, including when nothing has been voted on at all); a REData outage or
  missing configuration silently falls back to upload-recency ordering
- **REData label suggestions** — each profile's Tag and Category labels only (never Status,
  People, or Media) are synced to REData as a private per-profile taxonomy whenever they're
  created, edited, reparented, or deleted/converted away (retired, not hard-deleted), and a pin's
  complete current tag/category set is resynced whenever it changes, from any of the ~20 call
  sites that touch `Pin.labels` (`models.labels.signals`, the `Pin.labels` `m2m_changed` receiver
  in `models.pin.signals`, `services.labels.redata_suggestions`). The pin detail page's "Add
  Labels" dialog lazily loads a "Suggested for this place" section from REData's suggestion
  endpoint, scored against that profile's own vocabulary; a management command
  (`backfill_redata_labels`) primes REData with taxonomy/assignments that predate this
  integration. A REData outage or missing configuration silently disables sync and suggestions
- **Wiki article auto-seeding** — a wiki with no article yet is automatically started from a
  confidently-matched Wikipedia article the first time one is cached for its location (converted
  to Markdown, with a required CC BY-SA attribution footer linking back to the source) - never
  overwrites an existing article, seeded or human-written (`services.wiki.wiki_seed`,
  `models.cache.signals`)
- Place-name resolution across multiple sources (Google Places, OSM/Nominatim, NPS, **Azure Maps**, Wikipedia, OpenStreetMap) with agreement-based priority ordering, an admin-only drag-to-reorder priority list (Site Admin), and Google Places demoted to fallback-only (only considered when no other source has a candidate) - individual users cannot override the ordering
- Boundary drawing — property/building polygons per pin, generated automatically from a typed
  provider chain (`services.locations.boundaries.BoundaryProviderChain`) trying, in order:
  REData's authoritative county GIS parcel/building geometry (`RedataBoundaryProvider`, US-only,
  coverage varies by jurisdiction), then OSM/Overpass, Overture Maps, Microsoft Building
  Footprints, and Google Open Buildings; editable
  by the user
- Standalone reusable **MarkupMaps** with freehand drawing/annotation tools (point, line, freehand, arrow, text, box, circle, polygon), attachable to pins, wikis, safety check-ins, or kept independent; also embedded in the **safety check-in creation form** for drawing routes and destinations
- Detail pins — sub-markers placed inside a pin/wiki's bounding box for finer-grained mapping
  (rooms, entrances, hazards, etc.)
- **Georeferenced image overlays** — drop a historical map image (a Sanborn fire-insurance sheet,
  a site plan, an old survey) onto a pin's or wiki's map and drag its **four corners** until the
  old streets sit on the real ones. Four free corners means a full projective transform, so a scan
  that is rotated, sheared, or trapezoidal (as flatbed scans of century-old paper usually are)
  still lines up — an axis-aligned bounding box cannot express that. The image comes from an
  upload, a pick from that page's own Media gallery (materialized to a real `Image` first, so it
  survives the provider rotating its URL), or an external image URL. Per-overlay opacity, a lock
  to stop a placed sheet drifting, and either its own layers-panel toggle or membership in a
  custom layer (`models.map_overlay`, `controllers/map_overlays.py`,
  `frontend/ts/shared/map-image-overlays.ts`)

## External Data Enrichment (Pin Detail Page)

On-demand, cached lookups shown as panels on the pin detail page. Many of these are now backed by
REData (`../REData`, a standalone service reached via `UL_REDATA_API_URL`/`UL_REDATA_API_KEY`)
rather than calling their upstream provider directly - REData pools rate limits/credentials across
every UrbanLens deployment and normalizes each provider family's response shape. A handful of
integrations central to this app's own purpose (Nominatim, Esri, OpenWeatherMap/Open-Meteo, OSRM)
keep a direct implementation as a fallback for when REData isn't configured or fails; most others
were removed outright in favor of REData-only (no direct fallback); a few (Wikimedia Commons,
Nominatim's own OSM extratags, Azure Maps' geocode+POI panel, USGS Historical Topo Maps) stay
direct-only because REData's contract can't reproduce what they show:

- **Wikipedia** — best-matching article
- **Wikimedia Commons** — archival photos/media, direct (REData has no equivalent provider)
- **Smithsonian Open Access**, **Library of Congress**, **Internet Archive** — archival photos/media, via REData
- **Digital Commonwealth** (Massachusetts) — photographs, maps, and documents from MA libraries/museums/archives, via REData; Massachusetts pins only
- **Media previews** — Media-gallery items in formats no browser renders (archival TIFFs, scanned
  PDF inventory/nomination forms, HEIC) are rasterized to JPEG/PNG server-side rather than left as
  a broken tile or an anonymous document icon (`services.media.previews`). Remote sources go
  through a signature-gated endpoint (`controllers/media_preview.py`) so it can't be pointed at an
  arbitrary URL; the in-app REData proxies (CRIS attachments, LoopNet photos) render their own via
  `?preview=1`, passing already-displayable files straight through
- **Web Images** — broad web-image search across many engines (Flickr, imgur, Pinterest,
  DeviantArt, Openverse, Unsplash, …) via REData's web-search image mode, using an aggressive
  three-clause relevance query (all non-nickname aliases · state/country + municipality · the site's
  urbex/abandoned subject vocabulary) so a same-named place or operating business elsewhere is
  excluded (`plugins.builtin.searxng_images`)
- **National Park Service** (USA) — nearest park info, via REData
- **Yelp** — nearby business details, via REData
- **LoopNet** (USA) — commercial real-estate listings
- **Property Records** (USA) — county parcel ownership/tax/sale-history lookup, retrieved from
  REData via `RedataGateway`
  (`services.apis.property_records.redata_gateway`); populates the wiki's Ownership and Sale
  History cards with `OFFICIAL`-sourced records in addition to a details card. Coverage varies by
  county. **Owner names and contact details from those `OFFICIAL` records are subscriber-only**
  (`SiteFeature.PROPERTY_OWNERS`, enforced in `services.property.owner_access`) - the parcel, tax,
  assessment and district facts stay open to everyone, as do a user's own private `PinOwner` notes
  and any `WikiOwner` the community typed in themselves
- **USGS Historical Topo Maps** (USA) — historical topographic maps, direct-only (a gallery of
  individually-dated scans, a shape REData's imagery contract doesn't offer)
- **Nominatim/OpenStreetMap** — reverse geocoding and place metadata (two panels: Nominatim
  structured data, kept direct-only for its OSM extratags REData doesn't normalize; Photon
  nearest-feature lookup, via REData)
- **Regional Data** — US Census, Wildlife (iNaturalist), Seismic (USGS earthquakes), and EPA data
  loaded on demand per sub-tab; the Wildlife/Seismic/EPA nearby-facility lookups are via REData
- **Building Characteristics** — structured property/building data (appears for commercial and historic properties)
- **Buildings on this Property** — every structure standing on the parcel, with names and building
  numbers from REData (county GIS building-footprint layers plus NY SHPO CRIS), falling back to
  OpenStreetMap footprints inside the property boundary. Each row links to the sub pin covering
  that building, or offers to create the ones that have none (`plugins.builtin.parcel_buildings`).
  Also shown on the wiki page
- **News** — recent news coverage scoped to the location (appears for notable locations), via
  REData's GDELT-backed search
- **Underground Structures** — OSM-mapped tunnels, culverts, station levels, shafts and buried
  utility runs within 250 m, enterable features first, via REData (`plugins.builtin.redata_underground`)
- **Permits & Violations** (US cities) — the site's building-permit/code-violation/site-plan filing
  chronology with deep links to city records and plan drawings where published, via REData
  (`plugins.builtin.redata_permits`); flags when a dense block capped the result
- **Reported Incidents** (US cities) — block-scale police-incident reports from city open-data
  portals as visit-safety context, via REData (`plugins.builtin.redata_incidents`); traffic
  collisions excluded, block-scale location precision stated on the panel
- **Water & Hydrology** (USA) — streams, waterbodies, wetlands (USFWS NWI decoded) within 1 km and
  the containing HUC12 watershed, via REData (`plugins.builtin.redata_hydrology`)
- **Site Conditions** (USA) — NLCD land cover, EPA walkability index (incl. transit distance), and
  USDA SSURGO soil composition (dominant-first, no invented averages) folded into one panel, via
  REData (`plugins.builtin.redata_site_conditions`)
- **Air Quality** — current modelled readings (Copernicus CAMS, worldwide) with a count — never an
  average — of nearby community sensors, via REData (`plugins.builtin.redata_air_quality`)
- **OpenWeatherMap** — weather forecast; appears on Trip detail pages (keyed to activity location) and on the pin detail page when weather data is available. Via REData when configured, falling back to a direct OpenWeatherMap/Open-Meteo call
- **Sunrise/sunset & golden hour** — via REData when configured, falling back to direct Open-Meteo (its 5-day/3-hour OpenWeatherMap counterpart has no sunrise/sunset field), shown alongside the pin detail page's weather panel; golden hour is approximated as the hour after sunrise / before sunset
- Satellite imagery carousel: Google Maps and Esri (incl. up to 5 historical Wayback releases) are
  direct; additional providers (NASA GIBS, Mapbox, Bing Maps, OpenAerialMap, OpenTopoMap) via REData
- Street-view carousel: Google Street View is direct; Mapillary, KartaView, and Panoramax are via REData
- Debug overlay (admin-only) to inspect raw external-API responses per panel

All external integrations are cached (DB-backed, per-Location) and rate-limited per service, with
usage tracked in `ApiCallLog`/`ApiRateLimit` and toggled at `/site-admin/api-limits/`. A per-call
cost estimate (`ApiCallLog.cost_estimate`, from `ServiceDefaults.cost_per_call`) is logged for
services with a known published rate - `null` means "not priced," not "confirmed free," since
most services don't have a rate configured yet. Aggregated into a 30-day cost breakdown on the
site-admin API usage report and the public `/costs/` transparency page.

Beyond on-demand fetches, an hourly **background enrichment** task drips high-value lookups
(official names, aliases, street addresses, building boundaries) into whatever rate-limit budget
is left over after real traffic, spread evenly so multi-day quotas can't be burned in one day.
Sources are plugin-contributable (`EnrichmentSource`) and admin-tunable (run window, reserve
buffer, per-run caps).

## Extensibility: Plugin System

Third-party integrations are packaged as **plugins** (`dashboard/plugins/builtin/`) — see
`docs/designs/plugins.md` for the full contribution API. A plugin can add rate-limited services, pin-detail
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
- RSVP per member with trip-wide defaults and per-activity overrides; per-activity thumbs up/down voting on proposed activities
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

## Device Scanning

- Mobile app feature: scans for nearby wireless (Wi-Fi/Bluetooth) devices while a user walks a
  route, to help them notice a camera, sensor, or tracker they didn't expect. Uploads MAC address,
  signal-strength samples along the route, an estimated location, and an optional device-type guess
  through a single external-API endpoint (`device_scans:write`); a background task classifies the
  device (trusting the client's own guess, otherwise a small MAC-OUI/name heuristic) and, for
  camera/sensor/tracker types, updates a fuzzy map marker on every wiki (including child wikis)
  whose boundary contains the reported coordinates.
- Markers start as an imprecise area and get more precise automatically as more scans corroborate
  the same location, weighted toward recent activity over old; a device that appears to move shows
  up as two separate markers until the stale one ages out. The app can query which devices/signal
  strengths are already expected nearby (`device_scans:read`) and report back when an expected
  device wasn't detected, which — after a few consecutive misses — flips the marker to "presumed
  removed."
- Attribution to the uploader's account is a privacy preference (`track_device_scans`, Settings →
  History, default on) independent of authentication, which is always required; turning it off
  stores the same scan data anonymously instead of skipping it.
- **Individual scans are never retrievable through any API** — only the cumulative, unattributed
  marker per (device, wiki) is ever readable, and only for wikis the caller has already discovered.
- No manual marker-placement UI yet; markers are maintained entirely by the background pipeline.

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
- **Interaction preferences** — consent-style statements a profile can state about how they'd like
  to be treated, on or off the site (taking/sharing/tagging/using photos of them, friend requests,
  meetups, exploring with others, plus a free-text "other preferences" note). Shown on the public
  profile page purely for informational, consent-based interactions - nothing here is technically
  enforced yet (see `Profile.PREFERENCE_FIELDS`/`interaction_preferences`). Each choice field shares
  its base wording (`ConsentPreferenceWording` mixin in `models/profile/meta.py`) so options like
  "Please ask first" read identically across fields while each still declares its own member set.

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

Every surface where a user picks one or more labels (map filter sidebar, saved filters, bulk pin
edit, the quick-add-pin dialog, the label merge target picker, add-labels-to-pin/location/image,
organize page parent/child picker) shares the same search + kind-tab (Tags/Categories/Statuses/
All) filtering UX, backed by the `createFilterPicker`/`createChipPicker` factories in
`ts/shared/label-picker.ts` (or the equivalent bespoke picker where one predates those factories),
plus the `@mixin tad-tabs` / `@mixin tag-dialog-list` Sass mixins for consistent styling.

## Notifications

- In-app notification center (bell dropdown), mark read/unread, per-type delivery preferences
- Real-time push over WebSockets (`ws/notifications/`) with desktop `Notification` API support and
  a 60s polling fallback
- Outbound email notifications with per-role rate caps (hourly/daily/monthly) and safety controls
- **11-event × 4-channel notification matrix** (Settings → Account): each event type (new message, friend request, check-in alert, AI task completion, etc.) can be independently configured for in-app, email, WhatsApp, and SMS delivery. WhatsApp/SMS require a phone number on the profile. WhatsApp/SMS delivery is wired for every event type: DMs and safety check-ins keep their dedicated pipelines, and all other types dispatch centrally via a `NotificationLog` post_save signal (`services/notifications/notification_text_alerts.py`) — delayed 2 minutes, skipped if read in the meantime, debounced per type per 6h.
- **Native-app push** (`models/push_device`, `services/notifications/push.py`): a backgrounded app
  holds no WebSocket, so it registers a push destination instead. **UnifiedPush** — an app-chosen,
  self-hostable push server such as ntfy — is the default transport, matching the project's
  self-hosted ethos and keeping an F-Droid build free of Play Services. An FCM row kind exists for
  a future Play-Store flavour and is deliberately not dispatched yet
- Admin-only critical alerting via email + Gotify push (distinct from user-facing notifications)

## Custom Fields

User-defined private fields for **pins**, **photos**, **people**, and **maps**. Power-user feature for tracking non-standard attributes (e.g. access status, personal reference IDs, condition notes). Managed in Settings → Advanced.

## External Photo Integrations

- **Immich** — connect a self-hosted Immich instance (server URL + API key) to browse and import nearby photos linked to pins
- **Google Photos** — OAuth import from a connected Google Photos library
- **Flickr (personal library)** — connect your own Flickr account (OAuth1) in Settings, then search/import your own photos on a pin's Media tab (near this pin, on recorded visit dates, or all)
- **Flickr (public album import)** — pin and wiki Media: paste the public URL of *any* Flickr user's album/photoset (no OAuth needed) to preview and import up to 100 of its photos, with the same confirm-grid + progress-bar workflow as the other importers

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
- **External API keys** (Settings → Security → API keys): create/revoke/view API keys that let a
  third-party application act on the user's behalf with an extremely limited, scoped grant -
  currently reading only the owner's uuid and creating pins through the exact same
  `services.pins.pin_creation.create_pin_for_profile` path the map UI uses. Keys are hashed
  (never stored in plaintext, like backup codes) and revocation takes effect immediately.
  See `dashboard/external_api/` and the REST API section above.

## Undo / Data Safety

- Generic undo framework: deleting a pin, wiki, trip, safety check-in, saved filter, pin list,
  label, or markup map stashes a durable snapshot (on the ``UndoAction`` row itself, not in a
  cache) restorable for a retention window. Restores pre-check the constraints the recreate
  could violate and refuse cleanly rather than 500ing; relational pieces that were never part
  of the deletion (a list's member pins, a label's parents, a map's annotation authors)
  restore leniently, skipping whatever has since been deleted
- Settings → Undo History page to review and restore recently undo-able actions

## Achievements

Admin-defined awards users earn for contributing. An achievement is a **metric** plus a
**threshold** plus an icon, so new ones can be added at any time with no deploy — saving one
queues a backfill that grants it retroactively to everyone who already qualifies. Tiers
("10 pins", "100 pins") are just several achievements sharing a metric.

- **Where they show**: an Achievements section on every profile page, visible to exactly the
  audience that can see that profile (it reuses `Profile.can_view_profile`), plus a full
  catalogue at `/profile/<slug>/achievements/all/`. Progress bars toward unearned awards are
  shown only to the profile's owner — they would otherwise leak exact contribution counts.
- **Defining them**: Site Admin → Achievements (`/site-admin/achievements/`), or Django admin.
  Each award takes a name, description, metric, threshold, a Material Symbols icon name or emoji
  or an uploaded image, a colour, a display order, plus `is_active` (retire without revoking) and
  `is_secret` (hidden until earned).
- **Metrics** (`services/achievements/metrics.py`, extensible via `register()`): pins created,
  wikis created, wiki edits, photos uploaded, markup maps created, places visited, places rated
  by stars / vulnerability / danger (independently), trips planned, trips attended, comments
  written, friends, people invited who joined, and longest streak for each of the five streak
  kinds. Each metric documents its own exclusions (background draft wikis and externally sourced
  photos do not count, for instance).
- **Streaks**: consecutive days of logging in, uploading a photo, editing a wiki, pinning a spot,
  or commenting. One `ProfileActivityDay` row per profile per kind per day makes repeats within a
  day free, and `ProfileStreak` caches current/longest. Awards compare against **longest**, so
  breaking a streak never revokes what it earned.
- **Awards are permanent**: deleting pins lowers the metric but keeps the award.
- **Evaluation**: signals on the contributing models queue a narrowly scoped re-check (only the
  metrics that event could move) on transaction commit — and only when some active award actually
  measures one of them, so a site with no award on a metric does no background work when it
  changes. A nightly `sweep_achievements` task catches thresholds no write crosses, such as
  "trips attended" ticking up when a trip ends.

## Cost Tracking

Admin-defined running-cost accounting: depreciating hardware/infrastructure **components**
(a one-time replacement cost amortized evenly over a number of years, e.g. "Hard Drives / $1000 /
10 years") plus recurring monthly **operating costs** (e.g. "Electricity / $100/mo"). Either stops
counting via a `retired_at` timestamp rather than deletion, so all-time totals stay accurate.

- **Site Admin → Costs** (`/site-admin/costs/`): add/edit/retire/delete any number of components
  and operating costs, KPI cards (total recorded expenses all-time, average monthly expense,
  effective cost for the last 30 days, cost per active user), and a stacked monthly chart, all
  updating live via HTMX after every edit.
- **Combined with tracked API spend**: the "effective monthly cost" and every stat/chart merge
  admin-defined hardware/operating costs with the existing `ApiCallLog.cost_estimate` data (the
  same external-API cost tracking the site-admin API usage report is built from) into one figure,
  broken out by source (Hardware / Operating / External APIs) rather than shown as disconnected
  numbers.
- **Public transparency page** at `/costs/` mirrors the combined totals, cost-per-user, and chart
  for anyone to see — gated behind `SiteSettings.public_costs_page_enabled` (off by default,
  toggled from the Costs admin page); the page 404s until an admin turns it on, and a footer link
  appears only once it's enabled.
- Calculations live in `services/admin/cost_tracking.py`, reused by both the admin and public
  views so the numbers can never drift apart.

## Paid Subscriptions

Users can pay to hold a `SubscriptionRole` directly via Stripe Checkout, instead of waiting for an
admin grant. Per role, a site admin can independently enable any combination of:

- **Fixed price**: a flat $/month.
- **Pay-what-you-want (PWYW)**: the user picks any amount at or above Stripe's own $0.50 minimum;
  any nonzero pledge holds the role (e.g. a generic "support the site" role).
- **PWYW with a dynamic threshold**: same as above, but the role's features are only granted in
  billing cycles where the pledge meets or exceeds the site's *current* cost-per-user
  (`services.admin.cost_tracking.cost_per_user()`, the same figure shown on `/costs/`) - the
  "pay over the running cost to get VIP" case. Recomputed at each successful charge, on the user's
  own billing anniversary, so a pledge that used to clear the bar can silently stop granting
  access without the subscription itself changing status.
- A static PWYW minimum is also available as a non-dynamic alternative to the cost-per-user gate.

Managed from **Settings → Membership** (checkout, pledge updates, cancellation, and a link to
Stripe's hosted billing portal) and **Site Admin → Subscriptions** (per-role pricing). Stripe
webhooks (`/billing/webhooks/stripe/`) keep `RoleSubscription` status/pledge/threshold in sync;
a daily `sync_stripe_subscriptions` task re-syncs from Stripe as a safety net for missed
deliveries. `user_has_feature()`/`active_subscription_roles()` treat an active, threshold-met
paid subscription the same as an admin-issued grant. Service layer lives in `services/billing/`.

## Site Administration

- `/site-admin/` panel: user management, site-wide settings, usage stats (KPIs, system, API),
  subscription role management, per-service API rate-limit toggles, plugin inventory,
  achievement definitions, cost tracking, UI component showcase, dev toolbar (theme/map-dark-mode
  toggles, session reset)
- Data export/import tooling and on-demand/scheduled database backups
- Subscription roles grant feature flags (`SiteFeature`) per user; pending grants can attach to an
  email invite for users who haven't joined yet
- `/health/` returns a liveness response for Docker healthchecks and load-balancer probes
  (`controllers/health.py`, `AllowAny`) - the compose stack gates `app`/`app-ws`/`nginx` startup on it
- `/thanks/` credits page, rendering live contributor data pulled from the GitHub API
  (`controllers/thanks.py` via `services/apis/infra/github/contributors.py`)

## AI Integration

- Pluggable AI provider gateway (OpenAI, Cloudflare, Anthropic, Hugging Face). The AI chat
  assistant is pinned to Anthropic regardless of the site-wide provider setting, since its
  tool-calling protocol needs reliable instruction-following that smaller/free models don't
  consistently provide.
- AI-assisted import: extract pins from freeform documents/notes
- AI-assisted label styling: suggest colors/icons for auto-created labels
- Keyword-based and AI-assisted auto-tagging of pins/wikis
- **AI link extraction** — a per-link sparkle button (on the pin's Links card and inside
  external-data panels such as web search, Wikipedia, LoopNet, and news results) has AI read the
  linked page and extract allowlisted structured fields (date built, date abandoned, owner
  name/company, sale date/price, aliases) into the pin; the same run also asks a writing assistant
  for new plain-text paragraphs to append to the pin article and (when one exists) the location
  wiki article, after stripping all markup and a fail-closed safety AI review (no operational
  access/trespass guidance or inappropriate content); admin-settable per-user daily limit, a
  review page (`/ai/extractions/`) for results that couldn't be applied automatically, and a
  completion notification
- **Local keyword tagging** — entirely local (no AI or network call), keyword-match auto-categorize / auto-tag / auto-status on pin save; master toggle + per-type sub-toggles in Settings → Connections

## REST API

Two separate DRF surfaces - see `dashboard/urls.py` and `dashboard/external_api/__init__.py`
for the boundary rationale:

- **Internal, session-authenticated**, under `/dashboard/rest/`. Deliberately minimal - only what
  the app's own frontend uses: `pins` (PATCH/DELETE only - pin creation goes through
  `MapController.post_add_pin` instead, not this router) and a `reviews`
  `create_or_update` action for the star-rating widget.
- **External, API-key-authenticated**, under `/dashboard/api/external/v1/`. Lets a third-party
  application act on a user's behalf with an extremely limited, scoped grant - see "External API
  keys" under Account & Auth below. Independently versioned and never shares serializers/viewsets
  with the internal surface.

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
  shared between the check-in owner, every accepted partner, and every emergency contact. The
  session route additionally joins a narrower live-location group that contacts never join;
  removing a partner or a contact force-closes their open socket
- `ws/spotguessr/session/<id>/`, `ws/trivia/session/<id>/`, `ws/consensus/session/<id>/` — one
  channel-layer group per game session. Every state change stays a durable HTTP POST that
  broadcasts over the socket; the only client-to-server frame these accept is a chat message

## Games: SpotGuessr

A GeoGuessr-style game built on the user's own pin/wiki/photo data. Full design and phase
mapping: `docs/designs/drafts/spotguessr.md`. **Built (UL-391..UL-393): solo and multiplayer
play, all three guess modes.** Everything below the line is not yet built.

- Three modes: **Photos** (a photo shared to a pinned location's wiki - never a private,
  un-shared pin photo; guess by clicking a Leaflet map or searching your own pins), **Named
  Place** (a meaningful place name or, by default, a random alias/nickname - togglable off;
  map-click only, no pin search - the point is recognizing the name without being able to look
  it up), and **Street View** (imagery from the existing Street View integration, point-scored;
  map-click or pin search)
- Photos-mode community-relevance feedback: in-game thumbs up/down/report on the shown photo
  feed a blended relevance score (`services.media.media_relevance.effective_relevance`) alongside the
  wiki's own thumbs, weighted down for in-game signal (thumbs down at only a token weight - see
  the design doc's "Photo relevance feedback"); an "allow arbitrary external photos" setting
  (off by default) opts a session out of the relevance filter
- Crowd-sourced coordinates for still-unplaced photos: every guess against a Photos-mode photo
  with no coordinates of its own is anonymously recorded (no profile/round FK at all - just the
  guessed point, correct/incorrect, and a timestamp); 5+ correct guesses average into an
  estimated position (with a loose outlier trim past 10), shown on maps via
  `Image.effective_latitude`/`effective_longitude` until a real coordinate takes over - see the
  design doc's "Crowd-sourced photo coordinates"
- Eligibility engine: a location is only ever offered if it's pinned by every *joined*
  participant (an invited-but-not-yet-accepted player never gates this) — no exceptions, no
  caching across sessions
- Scoring: geodesic point distance when a photo/Street View shot has its own coordinates,
  geodesic distance to the location's effective property boundary (0 inside it) otherwise
  (always the boundary rule for Named Place) — real PostGIS `ST_Distance`, not an approximation
- Glicko-2 ratings, tracked per mode: player skill (`PlayerModeRating`) and location difficulty
  (`LocationModeRating`), updated after every round
- Difficulty slider (weights location selection toward a target difficulty band), a
  geographic-boundary restriction (draw a rectangle to confine rounds to an area), and
  anti-clustering location selection (never repeats a location in a session, avoids picking
  somewhere near the immediately preceding round). Locations with too little (or no) play
  history are seeded from proxies (pin count, photo count) instead of sitting at a flat neutral
  rating, so the slider has a real effect on unplayed locations from day one, not just
  well-worn ones
- Optional date-guessing bonus (guess the photo's capture date for extra points, default off,
  Photos mode only)
- Optional per-round timer (30/60/90/120 seconds, or untimed) - a live countdown auto-reveals
  the round when it expires, for either solo or multiplayer play
- Own Glicko-2 rating + friends' ratings on the overview page, with a per-profile opt-out
  (`SpotGuessrPreference.show_ratings_to_friends`, default on); each round's own rating change is
  now shown at reveal time (e.g. "▲ +14 rating"), plus a net-change-for-the-session total on the
  final summary screen alongside each player's best round
- Reveal-screen "feel": an animated point count-up, the guess-to-answer distance line drawing
  itself in rather than snapping into place, and a richer summary screen (winner callout for
  multiplayer, animated score-card count-ups)
- **Multiplayer**: a friends-only invite/join lobby (invite notification deep-links straight
  into the lobby) with a host-controlled start that locks the roster, a live scoreboard, and
  WebSocket-driven round sync (`GameSessionConsumer`, one group per session) so every
  participant sees rounds/reveals/results together in real time
- **Multiplayer stall handling**: a round stuck because a participant went AFK is force-revealed
  by a Celery beat sweep after 10 minutes (marking the session `ABANDONED` if literally nobody
  guessed), and the host can end an in-progress or not-yet-started game immediately at any time
  from an "End game" control - no more waiting out a dead lobby or stuck round
- **Live text chat** scoped to a session (WebSocket-only, no E2EE - unlike DMs, session banter
  between people already visible to each other on the scoreboard has no privacy surface to
  protect)

Not yet built: the community photo submission/moderation pipeline itself - upload-to-wiki with
a submit-to-game checkbox, a "submit this wiki photo to the game" lightbox button, and the
nudity/person moderation classifier (UL-394; in-game thumbs/report voting is built, see above) -
voice chat (UL-395), and a persistent site-wide leaderboard (UL-396; the live in-game scoreboard
and reveal/summary animation polish described above are already built). Also not built
(deliberate scope cuts, not oversights): join-by-link invites and mid-game joining - see the
design doc's "Multiplayer sessions" and "Multiplayer stall handling" sections.

## Games: Trivia

A quiz game built on the same pin/wiki/location data as SpotGuessr: answer questions about
places you've pinned, solo or with friends. Full design and phase mapping:
`docs/designs/drafts/trivia.md`. **Built (Phases 1-4): solo and multiplayer play, all three
question sources, AI content moderation, AI answer checking, and AI wiki incorporation.**
Everything below the line is not yet built.

- Three question sources, all gated by the same content classifier before reaching a player:
  **deterministic** templates from cached property-records data (year built, building number,
  and building count once a parcel has more than a few buildings - all only for named
  buildings), **AI-generated** from wiki articles with substantial content (up to 3 per wiki),
  and **user-submitted** questions about a location the submitter has pinned
- Content classifier (`services.trivia.classifier`): rejects a question about a specific
  individual - even one only referenced indirectly and never named (e.g. "the year *someone*
  did X" still centers a person), rejects bullying language, rejects references to a specific
  exploring group/crew/party rather than the location itself, and rejects anything not actually
  about the place. Fails closed on any AI unavailability. Used identically for user submissions
  and AI-generated candidates - same rules, same code path
- **No feedback loop for submitters**: a submitted question's approval/rejection is never
  disclosed to its author, so a rejected question can't be iteratively tweaked past the filter -
  except that a solo player's own not-yet-approved question may still surface to them, very
  rarely, in solo play only (never to anyone else, never in multiplayer)
- Answers are checked case-insensitively and stripped to alphanumeric first; on a mismatch, an
  optional AI fallback judges whether the answer means the same thing just phrased differently
  (gated on the AI subscription feature - without it, exact-match-only, never blocked from
  playing)
- Upvote/downvote/report voting on questions, with a small passive +0.05 "shown, no reaction"
  default per play - a question whose blended score goes negative (downvotes and reports carry
  real weight, unlike SpotGuessr's near-token photo-thumbs-down) drops out of rotation until its
  score recovers
- Glicko-2 ratings: player skill (`PlayerTriviaRating`, one per profile - no per-mode split,
  unlike SpotGuessr) and question difficulty (`TriviaQuestionRating`), updated after every round
  from a binary correct/incorrect outcome
- Difficulty slider, applied per-question (a location can host both an easy and a hard
  question) rather than per-location
- **AI wiki incorporation**: once a user-submitted question's community vote score crosses a
  threshold well above the bare rotation gate, an AI writing agent drafts a short paragraph
  folding the fact into the location's wiki article - reusing the same sanitize/safety-classify/
  append pipeline as link-based article expansion, not a separate one
- **Multiplayer**: a friends-only invite/join lobby with a host-controlled start, live scoreboard,
  and WebSocket-driven round sync (`TriviaSessionConsumer`, sharing its connect/relay skeleton
  with SpotGuessr's `GameSessionConsumer` via a common base class) plus live text chat
  (WebSocket-only, no E2EE, same rationale as SpotGuessr's session chat)
- Own Glicko-2 rating + friends' ratings on the overview page, with a per-profile opt-out
  (`TriviaPreference.show_ratings_to_friends`, default on)
- Four independent `SiteSettings` toggles gate content moderation, AI generation, AI answer
  checking, and AI wiki incorporation separately - turning any one off never bypasses moderation
  for the others, it just holds the gated content back until AI is available again
- **Multiplayer stall handling and leave/kick**: a round stuck because a participant went AFK
  is force-revealed by a Celery beat sweep after 10 minutes (marking the session `ABANDONED` if
  literally nobody answered), the host can end an in-progress or not-yet-started game
  immediately at any time, and any participant can voluntarily leave (or decline an invite) -
  or be removed by the host from the pre-game lobby roster - at which point the host role
  transfers automatically if the host themselves leaves

Not yet built: a moderation review UI for AI-rejected questions (the only way to inspect why a
question was rejected today is direct DB access) - explicitly decided against, not just
unbuilt - see the design doc's "Known gaps" section.


## Games: Consensus

The wiki-data-completion game, and the only game that writes back to shared data. A round shows a
player one missing or unconfirmed piece of data about a `Wiki` they have a visited pin for — a
name, description, alias, or a photo's coordinates — and the player supplies it. The per-field-kind
registry driving round generation and answer application lives in `services/consensus/fields.py`,
so a new answerable field is a registry entry rather than new game code.

- **Solo and competitive modes.** Solo applies an answer the instant it is submitted. The
  competitive mode races participants and resolves disagreement by vote; when a vote cannot settle
  it, the answer lands in a cross-session *tentative* pool that later sessions can confirm
  (`ConsensusTentativeAnswer`)
- **Trust, not just points.** `ConsensusProfile` carries a Beta-Bernoulli posterior
  (`trust_alpha`/`trust_beta`) updated from trust-check rounds — rounds whose answer is already
  known — starting from a weakly-informative prior so a new player is neither trusted nor
  distrusted. That posterior weights how much a player's answer counts. Points and levels are
  Consensus-only and deliberately not shared with SpotGuessr/Trivia's Glicko-2 ratings, and are
  awarded for out-of-game manual wiki edits too (`models/wiki_edit/signals.py`)
- **Session flow** under `games/consensus/`: home, friends, start, lobby, invite, join, begin,
  round, answer, vote, end — with `ws/consensus/session/<id>/` pushing round and resolution
  updates, and a stall sweep (`sweep_stalled_consensus_sessions`) reclaiming abandoned sessions
- Answers feed the same fact-confidence machinery documented under the wiki sections
  (`services/facts/confidence.py`), which converges a value by trust-weighted agreement clustering
