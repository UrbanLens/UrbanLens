# UrbanLens Features

A feature inventory of what UrbanLens currently supports, generated from a codebase audit
(2026-07-11, last verified/expanded 2026-07-29). This is a snapshot, not a promise — see the repo-root `ROADMAP.md` for what's planned or partially
built, and `docs/NOTES.md` for non-obvious behavior behind these features.

## Mapping & Pins

- Interactive Leaflet map with 9 configurable layers (Street, Terrain, Satellite, Weather, Dark,
  Borders, Places, Pins, Child pins), HTMX-driven panels, and a filter sidebar (labels, rating,
  visited status, date pinned, scores, saved filter configurations)
- **Map right-click menu** — every map shares the same base actions (copy coordinates, Street View
  when Google has coverage, directions to that point). The main map adds "Add Pin Here"; Private Pin
  and wiki maps add "Create child pin here". Clicking or right-clicking a parcel or building
  boundary on those pages also offers Edit, Convert to the other type, and Delete. The floorplan
  editor keeps its own specialised menu.
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
  A building with no known footprint draws the outline drawn on its wiki (`scope.outline_applies`,
  P264). On an ordinary single-building property neither distinction exists, so markers stay neutral and
  both outlines are drawn. Scope is derived from the place and applies to *every* user's marker on
  it; an explicitly chosen type always wins. A badge in the page header names the scope whenever it
  isn't the neutral default. See `docs/NOTES.md`.
- **A new root pin's property is fetched without anyone opening it** (`services.pins.bootstrap`,
  `tasks.bootstrap_location`) — creating a top-level pin (map dialog, external API) queues a staged
  chain: REData prewarm of the point (`POST /locations/prewarm/`, scope `locations:prewarm`; a key
  without it is remembered for a day and the chain goes on), the parcel boundary, the building list,
  the community wiki and the building pins and wikis, then the site panels (property records and
  ownership, historic registers, NY CRIS, news, web photos, incidents, the REData archives), and last
  the build dates. Each fetch takes the panel's own flight marker, so a page opened meanwhile polls
  the chain's fetch rather than starting one, and a stage that finds a page's fetch in flight waits
  for it. Without REData the boundary and buildings come from OpenStreetMap and the panel stage is
  skipped. Gated by the owner's external-lookups setting; community features and automatic building
  pins gate the wikis and pins as they always do. One campus costs a fixed 25 REData requests
  whatever its building count (`tests/hypothesis/test_pin_bootstrap.py`). Imports never bootstrap
  (they create pins through `Pin.objects.get_nearby_or_create`) and are left to the hourly enrichment
  job; so does a pin past its profile's ten bootstraps an hour, or the site's twenty. `manage.py bootstrap_pin <uuid|slug>`
  queues it for an existing pin
- **Build dates from records** (`services.pins.build_dates`) — a building pin takes its record's
  `year_built` when it is created, only when REData says the year is the building's own
  (`year_built_basis: "building"`, REData 0.3.6+; `own_build_year`). The assessor's year is the parcel's,
  for its principal improvement, and reaches at most the building under the parcel's lookup point; a
  record without a basis (older REData, a list cached before it) is read as the parcel's, so neither
  dates a building. The root pin takes the year of a register listing drawn around it, else of the
  building it stands on, else, unless it reads as a building itself, the assessor's, once the bootstrap
  has fetched them. Only an empty date is filled, stored as January 1st of the year. The same year is
  recorded as `built_year` evidence (`EXTERNAL_SOURCE`) on the place's wikis, and the wiki's About card
  shows "Built <year>"
- **Every building on a property becomes a child pin and a child wiki automatically**
  (`services.pins.auto_nest`, `services.pins.building_clusters`) — once a top-level pin's property
  outline is known and it holds several buildings, a background sweep creates one `building` sub pin
  per physical building, nested the way REData nests them (a chapel inside a hospital block sits
  under the block), and a matching child wiki under the place's community wiki, which the pin sits on
  so its hero link and wiki panel open that building's own wiki. Records describing one structure -
  an overlap REData left unresolved (`overlap_refs`), footprints that mostly coincide, markers
  within 15 m - collapse into one building, so no two sibling pins stand within 15 m; REData's
  `parent_ref` nesting always keeps a building apart from the one containing it. Child wikis take
  the building's public name, else "Building <number>", else its address, else a descriptor such as
  "Garage (1925) at Hudson River State Hospital", dated only by the building's own year (see Build dates
  from records) - never the campus's own name or a private pin name. A later sweep renames one given a
  placeholder before the campus was named, or a descriptor its records now date differently, including
  one dated before UrbanLens read `year_built_basis`. Each building pin's
  detail boundary is its own footprint, which the floorplan editor seeds as exterior walls. The sweep
  runs when the pin is created, when the building list is fetched or refreshed, when the property
  outline arrives, and when the Buildings panel shows an unpinned building (throttled to once per
  10 min per pin). It recognises its earlier pins by where they stood
  (`Pin.auto_nested_buildings`), not by REData `ref`, so a renamed ref duplicates nothing and a
  child you deleted or moved stays that way. Off with "Organize this property?" → no, the Pin
  Organization Suggestions setting, or a user-chosen building/entrance type on the pin itself; an
  owner with community features off gets the pins but no wikis, as with any pin they save
- **"Organize this property?"** — one suggestion, shown once the first time you open a pin's detail
  page, covering both halves of the same question: create a sub pin per building here (named and
  numbered from REData's county GIS + NY SHPO CRIS, or OpenStreetMap, and mirrored into the place's
  community wiki - seeding an invisible draft wiki if none exists yet, so the buildings are never
  silently dropped), and nest any of your existing *top-level* pins that
  stand inside the property boundary — useful for maps built before child pins existed. Nesting only
  re-parents; nothing is merged, renamed, or deleted. Three answers: yes, no (permanent for that
  pin, even if new buildings turn up later), or don't show again (Settings → Map → Pin Organization
  Suggestions). Buildings you've already pinned are detected by their real footprint polygon, not a
  fixed radius, so a pin at the far end of a long hall still counts as covering it
- **Tree reads in one query** (`models.abstract.tree.TreeQuerySetMixin`, on `PinQuerySet` and
  `WikiQuerySet`) — `with_descendants()` / `with_ancestors()` return a composable
  `pk IN (WITH RECURSIVE …)` queryset, `ancestors_of(node)` the ordered chain, `lineage_ids(node)` the
  keys that may not nest under a node (no query for a root), and `would_close_cycle`. Cycle-safe via
  `UNION`. Use these rather than walking `parent_pin` / `parent_wiki` level by level
- **Notes (pin comments) are never hidden by nesting** — the Private Pin page's "show sub pin
  details" toggle (`?children=1`) aggregates a child pin's private notes into its parent's Notes
  tab too, each labelled with a link back to the sub pin it was written on, alongside the map,
  photo gallery, and visit history the toggle already covered
- **A building child's own details on its property's page** (`services.pins.child_buildings`,
  `controllers/child_buildings.py`) — a building row in the property's Buildings on this Property
  list (a building a child pin covers, or a building in its Child pins tab) opens that child's card
  in place: the owner's description and dates, links to the building's page and wiki, and every
  info panel declaring `building_level` (Building Attributes, Building Characteristics, and
  Historic Preservation, which shows Historic Registers and CRIS together) fetched for the child, not
  the property. Nothing is fetched until the row is
  opened. The page-wide "child pin details" toggle (`?children=`, not a stored preference) starts on
  for a parcel and for any property holding exactly one building child, but not for a pin its owner
  typed as a building, whose building child is a structure inside it
- **The "child pin details" toggle swaps in place** (`PinController.child_details`,
  `LocationWikiChildDetailsView`, `shared/child-details.ts`) — on a pin's page and a wiki's page it
  fetches every panel that reads the setting (photos, visits, albums, notes, Article > Sources, and on a pin
  the aliases and labels) as
  out-of-band swaps, replaces the address's `?children=`, and announces `childDetailsChanged` so
  the map refetches its markup, detail pins, photo layer and outlines with `children=` set. With it
  on, the pin map draws each descendant building's own outline once
  (`Boundary.objects.own_polygons_for_pins`); off, none. A new panel that reads the setting needs a
  region in `_child_details_regions.html` and an id it keeps after loading. Panels that re-render after their own
  edit (notes, visits, aliases, labels) read the setting through `services/core/child_details.py`: the request's
  `children`, else the page's address in `HX-Current-URL` when that page is the pin's or wiki's own, else that
  page's default; a panel opened from any other page, such as the map, lists the target alone
- **Child pins' aliases and labels on the parent** (`services/pins/child_listings.py`) — with child pin
  details on, a pin's Aliases and labels panels list each descendant's aliases and labels read-only, after a
  `.child-chip` naming the child; editing stays on the child's page
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
  Nesting follows place lineage, so it agrees with access by construction, and runs whenever
  `get_or_create_for_location` creates a wiki (a pin, a share, an enrichment photo: P263) as well as
  when its boundary arrives; a wiki holding no place of its own looks for its container from the
  place its point stands on upward (P231). See `docs/NOTES.md`.
- **One wiki per place** — creating a wiki for a coordinate that already has one, however far apart
  the two coordinates are on the same property, returns the existing page instead of a second one.
  A viewer who has earned the page reaches it from their own location's URL. On a campus, a
  coordinate standing on one of its buildings (by footprint, else within 15 m and nearest) is that
  building's: it opens the building's wiki, or gets a new building wiki nested under the campus's
  (`services.wiki.building_wikis`, P261), read from places, wikis and the campus's cached building
  list, never pins; its location is named as a building's (`name_tiers.naming_scope`). Each cache of
  a campus's building list queues `tasks.ensure_building_wikis`, which gives the pinned locations then
  found on a wiki-less building their wiki (P265). The one
  creation path is `Wiki.objects.get_or_create_for_location` (`models/wiki/queryset.py`), which
  checks both one-to-ones (Location, then its Place) inside a savepoint and re-reads on a raced
  `IntegrityError`; a building's placeless wiki, with no unique column, is created holding the
  campus wiki's row, as the building mirror does. Nothing else should create a Wiki.
- Add pins by map click, coordinate entry, or place search/autocomplete; drag to reposition
- **Photos on a map** (`shared/photo-map.ts` `createPhotoMarkerLayer`, used by the pin/wiki map and
  album maps) - thumbnails that cluster into a stacked badge, open the shared photo lightbox on click,
  and drag to a new spot when the photo is the viewer's own. Highlighting (hover, the side panel) and
  `flash()` restyle the drawn icon in place, and a highlighted photo becomes its cluster's front photo.
  One drag type, `PHOTO_IDS_TYPE` (`shared/photo-tile.ts`), carries the viewer's photos out of the
  gallery, the Media section, album grids and the map's side panel; the pin map places whatever it
  receives where it is dropped.
- **Places layer** (a `SiteFeature.PLACES` feature) - a map click shows historical landmarks (REData or
  Google, zoom 10+), national parks and geotagged Wikipedia articles nearby, each source per the viewer's
  profile toggles. Snapped to a ~2 km grid cell and a radius bucket, fetched in parallel under one budget,
  and cached per source, so one source failing leaves the rest (`services/map/nearby_places.py`)
- Pin list view alongside the map (particularly useful while searching/filtering); "Add these pins to a list" bulk action from the pin list panel adds all currently-visible/filtered pins to a trip or saved collection at once
- Bulk pin operations: multi-select, bulk edit (description, rating, labels, parent pin), bulk merge, bulk delete (with undo). The web select-map toolbar and the external API both call
  `services.pins.pin_bulk` (`bulk_merge_under`, `bulk_delete_pins`, `bulk_edit_pins`), one atomic
  service per action so the two surfaces cannot drift; a merge or bulk reparent refits each
  affected parent's child-fitted boundary once at the end rather than once per moved pin
  (`services.geo.child_pin_boundaries.deferring_child_boundary_refits`)
- Per-pin alternate names (**aliases**) — private aliases on a Pin vs. shared aliases on a Wiki;
  names are unique per pin/wiki case-insensitively. Deleting an auto-added alias, link, label, or
  property owner is permanent - automatic sources (external name lookups, AI extraction,
  keyword/AI auto-tagging) won't silently recreate something you removed. A link a provider added
  (OpenStreetMap, EPA ECHO, Wikipedia, the National Register) records which in
  `PinLink`/`WikiLink.auto_source`, and its chip says "Added automatically from ..."; links added
  before that field existed are unattributed. Every alias
  get-or-create goes through `PinAlias`/`WikiAlias.objects.resolve_or_create`
  (`models/aliases/queryset.py`), which sanitizes the name the way `save()` will store it (NFKC,
  drops symbols/emoji, collapses whitespace) before the case-insensitive lookup, so a name that
  only differs after sanitizing is reused rather than colliding on insert; `Location` coordinates
  have the matching helper, `Location.objects.get_exact_or_create`
  (`models/location/queryset.py`), for a caller that must keep a submitted point exactly as given
  rather than snapping it onto a nearby Location the way `get_nearby_or_create`'s dedup radius does
- **Child pin and child wiki slugs** start with a short parent prefix (`HRSH` for Hudson River State
  Hospital, `ford` for Ford Motors, `switz` for Switzerland): for a pin, the shortest compact alias of the
  parent or one derived from its long name; for a wiki, one derived from the parent Location's provider
  name only. Trailing words that would overflow a readable length are
  dropped as a unit, including hyphenated compounds (`non-contributing`), rather than clipped
  mid-word; dropped words are added back only when the ideal slug is not unique or is too short
- **Wiki URLs** are `/location/<location slug>/wiki/...`; `/location/<slug>/` redirects (302) to the wiki.
  A Location's slug comes only from a provider's name for it, or its uuid: `Location.provider_name` is
  `official_name` when `official_name_source` records which provider supplied it. Client text, pin names
  and community wiki names never reach `official_name`, and a name of unknown origin (legacy rows the P186
  migration could not prove) mints no slug, names no new wiki, is never shown as official on the wiki or
  to a concealed viewer, and is never a shared search name (`search_names.shared_names`). The slug follows the current provider name (`Location._sync_slug_after_save`): a
  provider name arriving or changing re-mints it unless the old slug is one the new name could also give, a
  cleared name (or one left without a source) puts it back on the uuid, and the Location's wiki and that wiki's
  children are re-minted to match (`Wiki.sync_slug_with_provider_name`). A slug the Location gives up goes to
  `LocationSlugHistory` and is never minted for another Location; a re-mint takes back a former slug of the
  Location's own that the new name could give, so a provider flipping between two names moves between the same
  two slugs, and only the newest ten former slugs are kept (`MAX_FORMER_SLUGS`). Migration 0054 applied the same
  rule to rows renamed before it existed (P250). A wiki route reached by the uuid or a former slug answers a 301 to the
  same route at the current slug, keeping the rest of the path and the query string; the wiki routes resolve
  both too, so a POST or an API call at an old slug lands in place. Only a GET or HEAD from
  someone the wiki itself would serve is redirected (`Cache-Control: private, no-store`); anyone else gets
  the wiki's usual 404 (`redirect_to_canonical_location` in `controllers/location_wiki.py`). Who may see the
  Location is part of the one query that finds it (`wiki_access.visible_location_or_404`,
  `LocationQuerySet.from_url_slug`), so a Location the requester cannot see 404s after the same statements
  as a slug nothing ever used, on every wiki route, the external API's wiki routes, a pin relink and a
  markup map's title lookup; trip routes do the same with `TripQuerySet.visible_to`, and a trip activity's
  location reference finds only a Location its author may see or one already on that trip (P236). `Wiki.slug`,
  informational and not routed, follows the same rule. Pin slugs, scoped to and seen only by their owner,
  still come from `Pin.effective_name` and the parent pin's aliases
- Private per-pin notes (`PinNote`), independent of public comments
- **Articles** — Wikipedia-style long-form write-ups (sections, links, references) with full
  **revision history** (every saved version stored, restorable from the Edit History tab); private
  per-pin, or shared/community-editable per-wiki. Edited via a WYSIWYG canvas (click-to-format,
  no Markdown syntax required) with a Markdown "Source" mode for power users/footnotes - saved as
  plain Markdown either way. The canvas writes back only the top-level blocks the user changed;
  every other block, and the blank lines and link definitions between blocks, keep their exact
  source. Blocks the canvas can't model faithfully (raw HTML, footnote definitions, `#` headings)
  show as protected source, editable in Source mode (`frontend/ts/shared/article-source.ts`)
- **Article > Sources** — a sub-tab on both the private pin page and the wiki page listing the
  documents cached for the place (the CRIS inventory forms and nomination PDFs, each naming
  its building on a campus, and the PDF and DjVu books and reports Wikimedia Commons and the
  REData archives find that pass the media relevance rule), viewable in a same-origin iframe or a new tab. Any cache-backed panel
  becomes a source by subclassing `DocumentPanelSource`; the PDFs are served by a proxy scoped to
  what that pin's or wiki's own list names, and only bytes that really are a PDF
  (`controllers.article_sources`, `services.pins.source_documents`). A source whose document has
  its own archive page sets `SourceDocument.page_url` and links there instead of proxying
  (`DocumentMediaPanelSource`, used by Commons, whose scans run to tens of megabytes, and the five
  REData archives). Wikipedia articles and Wikidata entities near the pin (REData's near-point
  `/reference-documents/`) are listed too, each judged by the relevance rule, without the article
  the Wikipedia panel already shows (`plugins.builtin.redata_nearby_documents`). The tab fetches
  CRIS, Commons and the Wikipedia/Wikidata list itself; a source built with
  `fetched_for_sources=False` (the archives) lists only what its Media gallery already cached, so
  opening Sources costs no archive searches
- Pin sharing — share a single pin with one friend, including re-share chains; every share
  records a provenance chain (`LocationExposure`) of how a location reached each user.
  `services.sharing.pin_sharing.create_pin_share` (gated by `require_pin_owner`) is the single
  path every caller, web and messaging alike, goes through to create one. A sent markup map records
  the sender's pins it calls out and, up to five per send, the places it marks where they have none (a marker or
  label's point, a property-sized circle's centre), as location-only shares
  (`services.sharing.map_sharing.share_markup_map_with_profile`, P21), as a coordinate typed into a message does
- Import: Google Takeout (Saved Places, Location History, My Activity), GPX, GPX tracks, OSM XML,
  Shapefile, WKT/WKB, KML/KMZ; AI-assisted import from freeform documents/notes
- Targeted export of a pin selection (main map's multi-select toolbar) or a whole saved list
  (a list's "more actions" menu) as GeoJSON, KML, GPX, or CSV (a CSV text cell that opens with `=`, `+`,
  `-`, `@`, a tab or a carriage return gets a leading `'` so a spreadsheet reads it as text)
- Data export/import of a user's full dataset, plus scheduled/on-demand backups. The archive
  carries safety check-in history, map annotations, saved searches/routes, pin aliases, and the
  profile's contact/social fields - all importable, with deliberate exceptions: live-status
  safety check-ins never import (a restore must not re-arm reminders), and secondary emails never
  import (verification state must not transfer). Export files are streamed: each exporter reads with
  `.iterator(chunk_size=EXPORT_CHUNK_SIZE)` and writes through `export.JsonArrayFile`, which appends
  one element at a time and produces the bytes `json.dump(indent=2)` would. A photo or overlay
  image media storage refuses on import is set aside with every file after it, the rest of the
  archive is imported, and the job runs again for just those files (1, 2, 4, 8, then 15 minutes
  apart, the import status saying storage is unavailable meanwhile); after five refusals in a row
  with nothing stored between them, the summary lists the files left and asks for the archive again

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
  and a plain-text fallback when no structured interpretation matches. Pins and wikis also
  match by external tag ("restaurants", "pin with tag restaurant" — restricted to pins by the
  existing "pin" type keyword), including any provider's equivalent tag once admin-grouped —
  see "Tag equivalence mapping" below. The map's own "Jump To" pin search bar matches the same
  way
- **Typed search operators** (`type:`, `place:`, `near:`, `visited:`/`created:`/`updated:`, …) —
  the same query box also accepts exact `key:value` syntax, unrecognized keys fall back to plain
  text rather than erroring. `label:`/`-label:` narrows/excludes by Label (pins, photos, wikis
  only — the only types with one); `by:`/`author:` (`by:me` or a name) filters by who created
  it, per type's own notion of authorship; `has:`/`-has:` (pins only — photos, comments, visits,
  notes, labels, links, floorplan, markup, wiki, check-ins) and `is:` (`visited`/`unvisited` on
  pins, `upcoming`/`past` on trips, `archived` on safety check-ins) filter by attached content or
  state; `sort:` (`recent`/`created`/`updated` on everything, `visited` on pins/visits,
  `most-visited` and `nearest` on pins) reorders a section instead of the default relevance
  ranking. A choice with no real backing anywhere (`is:starred`, `has:coords`, …) explains itself
  next to the results rather than silently returning nothing

## Lists & Saved Filters

- **Pin lists** — ordered, slug-addressed collections of pins with their own detail page
  (list-scoped map using the shared toolbar/layers, drag-to-reorder, bulk add from the current
  map filter); create a trip from a list, add a list's pins to an existing trip, or generate a
  markup map from one
- **Smart lists** — lists auto-populated from saved-filter criteria and resynced automatically
  as pins and labels change. A change records a `SmartListSyncRequest` and one queued sync per
  account applies it (`services/pins/smart_list_sync.py`); until it runs, the list's page and the
  external API's `membership_pending` say the list is catching up
- **Saved filters** — reusable filter configurations with full CRUD (managed alongside lists at
  `/lists/`), name suggestion, live match counts, and geographic include/exclude polygon
  regions selected via boundary search; usable from the map's filter sidebar and as smart-list
  criteria. `SavedFilter.matching_pins()` is a filter's pins as an unevaluated queryset;
  `PinQuerySet.matching_saved_filters(filters)` ANDs several in as SQL subqueries, and
  `SavedFilter.objects.for_client_ids(profile, raw)` resolves posted uuids, dropping malformed and
  foreign ones

## Locations & Community Wiki

- **Location** — shared, address-authoritative record for a physical place; coordinates are
  immutable after creation (mutable address/geocode metadata only)
- **Wiki** — opt-in, community-editable page for a Location: description, aliases, community
  danger/vulnerability/rating stat voting (`WikiStatVote`, fuzzed community counts for privacy),
  edit history with revert (`WikiEdit`). A boundary edit (web, external API, or a revert) is
  written and reverted through `services.geo.wiki_boundary_edits` (`save_wiki_boundary`,
  `revert_boundary_change`), which compares geometry rather than WKT text and stores each drawn
  outline once as an immutable `BoundaryRevision`, referenced by id from `WikiEdit.changes` -
  consecutive edits that redraw the same outline share one revision instead of duplicating it
- **Wiki Media gallery** — the Private Pin page's combined Media section, mirrored on the wiki
  (`controllers/wiki_media.py`): the same external providers (Wikimedia, Smithsonian, Library of
  Congress, Internet Archive, Web Images (SearXNG), Yelp, Google Images/Maps, LoopNet, CRIS, …)
  appear automatically
  from the shared per-Location cache (the shared row only, never one cached for a pin's own names), alongside a
  "Photos" tab of images intentionally shared to
  the wiki (`Image.wiki`) and a "Manage" tab for uploads. Thumbs-up/down are **community votes**
  (net score up − down, highest ranked first); because relevance is stored per-Location
  (`MediaRelevance`), a relevance mark made on any user's Private Pin page already counts here
- **Media subject relevance** — every text-searched gallery source (`GalleryMediaSource.judges_relevance`:
  the archive providers, Flickr, Web Images, and Google Images, searched by address) keeps only items that are about the place, judged at
  read time on the pin Media gallery, the wiki gallery, the Photos tab and the external API: geolocated
  in the place's box, or naming it with a consistent ZIP/city/county/state, or a distinctive name with
  nothing contradicting it; a generic name ("Historic Mansion") needs a local indicator. Conflicts come
  from a GeoNames gazetteer (`services.geo.gazetteer`, CC BY 4.0), bundled from `geonamescache` by
  `bun run gazetteer:build`. An item the viewer
  marked relevant, or the wiki voted above zero, stays (`services.media.subject_relevance`). A scheduled sweep
  (`tasks.sweep_public_media_cache`, `services.media.public_media_sweep`) then removes from the cached rows
  whatever no reader is shown and nobody marked or copied, once per fetch and again whenever
  `subject_relevance.RULE_VERSION` is bumped
- **Remove from my results** — the Private Pin page's lightbox, for Media gallery and Photos > From public
  sources items, offers Relevant / Not relevant votes and a private hide: a `MediaRelevance` row with
  `is_vote=False`, which hides the item from that account's pin pages and counts toward no score
- **REData photo relevance scoring** — every new photo (upload, Google Places business photo
  backfill, or Media-gallery item materialized via "mark relevant"/"send to wiki") is submitted to
  REData's photo-scoring service with whatever signal is available (capture/location coordinates,
  capture date, uploader/photographer, wiki abandonment date); REData returns a calibrated
  confidence ("is this really a photo of this place") cached on the `Image` row
  (`services.photos.redata_relevance`). Relevant/not-relevant votes on a materialized photo are
  forwarded too, as REData's training signal - never as a scoring input. The Private Pin page's own-
  photos preview and the wiki's Photos tab order by this confidence (vote score first on the wiki,
  confidence breaking ties, including when nothing has been voted on at all); a REData outage or
  missing configuration silently falls back to upload-recency ordering
- **REData label suggestions** — each profile's Tag and Category labels only (never Status,
  People, or Media) are synced to REData as a private per-profile taxonomy whenever they're
  created, edited, reparented, or deleted/converted away (retired, not hard-deleted), and a pin's
  complete current tag/category set is resynced whenever it changes, from any of the ~20 call
  sites that touch `Pin.labels` (`models.labels.signals`, the `Pin.labels` `m2m_changed` receiver
  in `models.pin.signals`, `services.labels.redata_suggestions`). A write that skips `post_save`
  (the default-label bulk seed's `bulk_create`) queues the batch instead, via
  `services.labels.redata_suggestions.queue_label_definitions_sync`, which groups the saved labels
  by owning profile and queues one definitions call per group rather than one per label. The
  Private Pin page's "Add Labels" dialog lazily loads a "Suggested for this place" section from
  REData's suggestion endpoint, scored against that profile's own vocabulary; a management command
  (`backfill_redata_labels`) primes REData with taxonomy/assignments that predate this
  integration. A REData outage or missing configuration silently disables sync and suggestions
- **Wiki article auto-seeding** — a wiki with no article yet is automatically started from a
  confidently-matched Wikipedia article whenever one is cached for any of its place's Locations,
  and each pin's article when a match first replaces a miss (converted to Markdown, with a
  required CC BY-SA attribution footer linking back to the source) - never overwrites an existing
  article, seeded or human-written (`services.wiki.wiki_seed`, `models.cache.signals`). A campus
  building's wiki (nested, holding no place or one of several buildings' places) takes neither the
  article nor its link: the match at its point is its campus's or a neighbour's; a seed it took as a root
  goes, untouched, when it is nested (`wiki_seed.takes_wikipedia_article`, P262). The match
  is looked up from public data only - the Location's official or wiki name and its address,
  backfilled first for a coordinate-only pin - never a pin's own name
  (`plugins.builtin.wikipedia.public_name_hint`, `match_address_components`)
- **Automatic public wiki naming** — a community wiki is renamed from the Location's cached public
  names (Wikipedia, REData/CRIS, OSM, official name, Google last) only while its name is
  provisional: a placeholder, or an automatic Google/official-name stand-in. A name a person wrote
  is never replaced, a pin's own name is never a candidate, and each adopted name is kept as an
  official alias credited to its source (`services.wiki.wiki_naming.adopt_public_name`). A
  building's own location (only child pins or a child wiki stand on it) is named by its own records:
  CRIS's record when its point stands on the building (ahead of REData's building name, as the
  child pin was named from it), a listing that is the building's own (P230's rule); never the
  Wikipedia article found at its point or a listing that merely holds it, which are the campus's.
  A CRIS card landing refreshes the names (`name_tiers`, `register_names`, `name_resolution`,
  `models.cache.signals`, P231)
- Wiki access is gated by one reusable check, `services.wiki.wiki_access.wiki_accessible_to` (a
  child wiki resolves through its parent); every access-sensitive read or write path, including
  undo/redo, is expected to call it rather than re-deriving visibility
- Canonical admin-area spellings for comparing places from different geocoders: `services.locations.naming.canonical_state` ("New York" and "NY" compare equal) and `services.locations.display.canonical_country` (every USA spelling as one)
- Place-name resolution across multiple sources (Google Places, OSM/Nominatim, NPS, **Azure Maps**, Wikipedia, OpenStreetMap) with agreement-based priority ordering, an admin-only drag-to-reorder priority list (Site Admin), and Google Places demoted to fallback-only (only considered when no other source has a candidate) - individual users cannot override the ordering
- Boundary drawing — property/building polygons per pin, generated automatically from a typed
  provider chain (`services.locations.boundaries.BoundaryProviderChain`) trying, in order:
  REData's authoritative county GIS parcel/building geometry (`RedataBoundaryProvider`, US-only,
  coverage varies by jurisdiction), then OSM/Overpass, Overture Maps, Microsoft Building
  Footprints, and Google Open Buildings (asked only where its v3 release has coverage: Africa,
  South and Southeast Asia, Latin America; P319); editable
  by the user. Overture comes from REData's own Overture mirror where it holds the point and from
  Overture's public release elsewhere (`services.apis.locations.boundaries.overture.OvertureProvider`)
- Standalone reusable **MarkupMaps** with freehand drawing/annotation tools (point, line, freehand, arrow, text, box, circle, polygon), attachable to pins, wikis, safety check-ins, or kept independent; also embedded in the **safety check-in creation form** for drawing routes and destinations
- **Editing in the map composer** (the "Attach a Map" / "take a screenshot" dialog,
  `partials/map/_markup_composer_dialog.html`, `frontend/ts/shared/markup-composer.ts`) — everything
  drawn stays editable: click or tap a shape to select it, then drag it, pull its handles to move,
  add (the "+" on each edge) or remove points, extend a line or arrow from either end, scale it from
  its corner, or turn it from its stalk; change its colour, width, fill, or a label's words, size and
  turn. Delete/Backspace (never while typing), the Delete button, or the Layers list remove it; the
  Layers list also selects, hides (a hidden shape is not saved or downloaded) and reorders.
  Undo/Redo cover every edit, and Undo, Redo and Clear are disabled when they would do nothing. The
  map itself turns (leaflet-rotate, loaded only for this dialog); the saved map keeps its
  `bearing` and reopens and downloads turned
- **Per-area basemap credits** — an Esri basemap's footer credits the providers for the area and
  zoom on screen (Esri's `static.arcgis.com/attribution/<service>` coverage file, as esri-leaflet
  does), not the worldwide list; "Powered by Esri" always shows, and the static credit stands in
  until the coverage arrives or if it cannot be had. Downloads burn the same credit into the image
- Detail pins — sub-markers placed inside a pin/wiki's bounding box for finer-grained mapping
  (rooms, entrances, hazards, etc.)
- **Georeferenced image overlays** — drop a historical map image (a Sanborn fire-insurance sheet,
  a site plan, an old survey) onto a pin's or wiki's map and drag its **four corners** until the
  old streets sit on the real ones. Four free corners means a full projective transform, so a scan
  that is rotated, sheared, or trapezoidal (as flatbed scans of century-old paper usually are)
  still lines up — an axis-aligned bounding box cannot express that. The image comes from an
  upload (reusing a file you already uploaded to this pin, rather than failing as a duplicate), a
  pick from that page's own uploaded photos (the full gallery, including child-pin photos - not a
  short preview of recent ones), or a pasted image URL - **downloaded once and stored**, never
  referenced live, so the overlay renders the same for every later viewer and nobody's browser
  fetches the original host. After adding, the overlay appears on the map with corner handles so
  it can be pinned and warped immediately. Per-overlay opacity, a lock to stop a placed sheet
  drifting, and either its own layers-panel toggle or membership in a custom layer. Every entry
  point (the form, the historical-map picker, the archive importer) creates an overlay through
  `services.map.image_overlays.create_overlay`, which locks the owner row and enforces the
  12-per-map cap against the whole map regardless of viewer, and `image_from_external_url`, which
  downloads a pasted URL through the same `materialize_media_item` pipeline an upload uses
  (`models.map_overlay`, `controllers/map_overlays.py`, `frontend/ts/shared/map-image-overlays.ts`)
- **Georeferenced historical map overlays** — "Browse georeferenced historical maps" in the same
  manage-overlays dialog lists REData's community-georeferenced sheets covering the location
  (Sanborn plans, cadastral atlases, panoramic views placed by real control points via Allmaps/Map
  Warper) and adds one as a pre-placed, warped **tile** overlay - no corner dragging needed, same
  opacity/visibility/layer controls. Tiles stream through UrbanLens's own authenticated proxy
  (`controllers/historical_map_tiles.py`; 200s and definitive 404s cached, institutional outages
  never cached) so REData's API key stays server-side. An imported tile overlay naming such a sheet
  (another deployment's route, REData's own tile URL) is rebuilt onto this route; one naming any
  other host is drawn through this site and each tile kept once fetched
  (`services/map/remote_tiles.py`, `controllers/remote_tiles.py`). Below the covering sheets the
  dialog lists the catalogued map volumes of the place (REData `/maps/volumes/`: a town's
  fire-insurance atlas, a county atlas, found by catalogue place rather than georeference) with
  years, sheet and placed counts and a link to the institution's scans; a volume's placed sheets
  nearest the spot that the list above doesn't already offer can be added the same way. Asked only
  when the dialog opens, cached a day per point unless REData is still listing a volume's sheets
- **OpenHistoricalMap time slider** (beta) — a compact time slider below the map on Private Pin and
  wiki pages lets a beta user scrub through years and see OpenHistoricalMap's dated vector data
  (roads, buildings, land-use tagged with `start_date`/`end_date`) overlaid on the live map, for
  locations where OHM has nearby dated coverage. Gated by `SiteFeature.BETA_FEATURES`; the slider
  is hidden entirely, not shown disabled, when there's no coverage or the viewer lacks beta access
  - a deliberate stopgap ahead of REData's own future temporal-imagery endpoints (see
  `plugins.builtin.satellite_imagery`'s module docstring for the pattern this follows). With REData
  configured the slider reads REData's `/historical-features/` instead of OHM: one answer per point,
  shared with the Historical Features panel, and each year is a local filter over it
  (`services.locations.temporal_imagery`). Without REData, see
  `services/apis/locations/open_historical_map.py`


## Building Floorplans

Interior structure for one building: walls are the only drawn geometry, and everything else derives
from or hangs off them. Absent by default - most buildings will never have one, so nothing loads a
floorplan alongside a building; they answer only through their own endpoints
(`controllers/floorplans.py`).

- **Wall-first data model** (`models/floorplans/`) - a room is not a polygon but a named seed point
  that binds, at render time, to whichever enclosed region of the wall graph contains it (the
  face-derivation in `frontend/ts/shared/floorplan/planar.ts`), so a partition shared by two rooms
  exists once and moving a wall can never destroy a room's name, photos or labels. `FloorplanWall`
  segments (exterior / interior / virtual - a dashed hint for a boundary that isn't physically there
  / collapsed) carry `FloorplanOpening`s as intervals along themselves (`t_start`/`t_end`, not their
  own coordinates) so a door or window cannot outlive its wall or drift off it; `FloorplanLock` rows
  hang off an opening. `FloorplanMarker` covers point features (hazard / stair / elevator), with
  `connector_id` tying a stair or shaft to its counterpart on other storeys and an optional
  `linked_pin` making a marker a real detail pin elsewhere on the site. Every item draws from
  per-plan **source** and **reference pools** by uuid, and may carry the owner's labels.
- **Versioned whole-document, by date** — a layout change (a renovation, a fire) is a *new*
  `Floorplan` row with a later `valid_from`; the undated baseline is in force from the beginning of
  time. `?date=YYYY-MM-DD` answers "as of then", no date answers current
  (`models/floorplans/queryset.py`)
- **Geometry is plan-local metres, not WGS-84** - one origin per plan, shared across its floors so
  storeys stack and align, with lengths/angles/right-angle snapping computed as ordinary metres
  instead of needing a latitude correction at every step. Conversion to WGS-84 happens only at the
  edges - map rendering and the GeoJSON endpoint below (`services/floorplans/features.py`, mirrored
  by `frontend/ts/shared/floorplan/coords.ts`)
- **Visual editor** (`/map/pin/<slug>/floorplan/`, `frontend/ts/entries/floorplan-editor.ts`) — draw
  walls over satellite imagery or a georeferenced blueprint overlay, with leaflet-rotate squaring
  the view (and the snap grid) to a building that isn't north-facing. Seven tools - select, box
  select, rotate, wall, opening, room, marker - each with its own options panel beside the toolbar
  rather than behind a modifier key, since a modifier cannot be seen and does not exist on a phone.
  Rooms are *derived*, not vertex-edited: dragging a wall's corner, a corner it shares with other
  walls, its whole body, or a room by its fill (propagate / rigid-move / detach modifiers) is how
  geometry changes, and enclosed regions recompute from that. A room owns the partitions on its
  boundary but never the building's shell, and a corner resting on a wall the room does not own
  slides along it rather than dragging it. An outline nothing subdivides is the building, not a
  room, and is captioned only if someone names it deliberately.
- **What an opening is, in detail** — kind (door / doorway / gate / window / hatch), which way a
  door swings, drawn as the plan symbol; how high its sill sits; and the locks fitted to it, each
  with its own type, engagement state and the description/condition/material every item carries.
  Openings drag along their wall and onto a different wall, keeping the metre width they were given
  rather than the fraction. Storeys carry their own floor-to-ceiling height and height above sea
  level
- **Floors as a stack** — add above or below (so a basement is drawable), duplicate a storey
  directly above the one it came from, delete from the middle and have the rest renumber, and name
  a floor without losing the number that says which storey it is. No manual Save button - autosave
  debounces every edit, backed by an undo stack that takes a typed name back whole and a drag back
  on its own. A plan saved from another tab stops autosaving and offers a reload rather than
  overwriting it. A new floor seeds its exterior walls from the storey nearest it, or the building's
  real footprint when there is none. Versions are switchable in-page, and "Save as new version"
  forks rather than overwrites
- **Personal by default, shared on purpose** — a plan records where the doors are, what locks them
  and what opens those locks, so local plans are scoped to their author. "Publish to wiki" copies a
  version onto the place's community wiki (the author keeps their own), after which anyone who can
  see that wiki can see and edit it, each save recorded as a `WikiEdit`. Resolution order is: your
  own plan, then the community one, then REData's (external, none exist upstream yet)
- **GeoJSON feature endpoint** (`/map/pin/<slug>/floorplan/features/`) — the map-facing counterpart
  to the document, mirroring REData's: flat features filtered by viewport (`?bbox=`), storey
  (`?level=`; the ground floor when omitted, every storey for `all`) and element kind (`?kind=`),
  capped and reporting `truncated` rather than silently cutting off. The viewport is projected to
  plan-local metres and filtered on the item coordinates in SQL (no spatial index), and the cap is
  a `LIMIT`, so a read costs the rows returned rather than every row on the floor. Every feature carries the
  uuid it can be edited by, so anything clicked on a map is findable in the document
- **Photos attach to anything, and pool once** — the item details block offers this pin's own
  photos as thumbnails, and attaching cites a per-plan **reference** row rather than the image, so
  one photo attached to a wall, a door and its lock exists once and a photo nothing cites any more
  leaves the pool. Attaching never writes to the image: what is *not* offered is setting a photo's
  own coordinates or heading, because those cannot yet be set without overwriting what its EXIF
  reported, and that provenance question wants answering first (see the KNOWN OMISSION in
  `models/images/model.py`). **Source** rows work the same way for where a plan came from
- **Historical aerial captures in the satellite carousel** — REData's `/imagery/timeline/`
  contributes dated frames alongside current imagery, so a site that has been demolished,
  re-roofed or cleared can be seen as it was. Continuous satellite ranges are deliberately not
  listed as slides: they are dates to materialise on request, not images that already exist
- **Extra basemap layers from REData** — `/tiles/sources/` layers are registered alongside the
  built-in ones and served through an UrbanLens proxy, so REData's key stays server-side
- **Street and dark basemap chain** — vector where the map can draw it: our own Protomaps style with
  its tiles from Protomaps' hosted API (production and staging, browser-direct), falling back to our
  self-hosted tiles for the session when those fail; our self-hosted tiles alone elsewhere; and
  OpenFreeMap's keyless Positron and Dark styles for an installation with no basemap of its own.
  Where a map cannot draw vector, Esri's street and dark canvas rasters. The credit line follows
  whichever tier is drawn
- **Distress signals on the property card** — recorded liens/fines and tax delinquency from
  REData's `/parcels/{uuid}/liens/` and `/parcels/{uuid}/tax-payments/`. For this application
  they are the most telling records on the card: an open code-enforcement lien and years of
  unpaid tax are what "abandoned" looks like in public records

## External Data Enrichment (Private Pin Page)

On-demand, cached lookups shown as panels on the Private Pin page. Many of these are now backed by
REData (`../REData`, a standalone service reached via `UL_REDATA_API_URL`/`UL_REDATA_API_KEY`)
rather than calling their upstream provider directly - REData pools rate limits/credentials across
every UrbanLens deployment and normalizes each provider family's response shape. A handful of
integrations central to this app's own purpose (Nominatim, Esri, OpenWeatherMap/Open-Meteo, OSRM)
keep a direct implementation as a fallback for when REData isn't configured or fails; most others
were removed outright in favor of REData-only (no direct fallback); a few (Wikimedia Commons,
Nominatim's own OSM extratags, Azure Maps' geocode+POI panel, USGS Historical Topo Maps) stay
direct-only because REData's contract can't reproduce what they show:

- **Wikipedia** — best-matching article
- **Historical map picker** — georeferenced sheets covering a pin (Sanborn fire-insurance plans,
  cadastral atlases, panoramic views) added as map overlays; each row shows the institution's own
  thumbnail and links its catalogue page, and discloses a loose georeference ("placed to ±60 m")
  where that figure is meaningful (`controllers.map_overlays`)
- **National Park Service** — the nearest NPS unit, with what it costs to get in and when it is
  open (day-grouped from NPS's published hours), its directions page, designation and activities
  (`plugins.builtin.nps`)
- **Recorded weather on a visit** — each row in a pin's Visit History says what the weather actually
  was that day (ERA5 reanalysis via REData, worldwide, back to 1940). The panel reads stored days only
  and queues `fetch_recorded_weather_at` for the rest, clustered by date so a page of visits costs one
  request per place rather than one per visit. Days are stored one row per 0.01° cell and day
  (`RecordedWeatherDay`), shared by nearby places (`services.locations.visit_weather`)
- **Historic Registers** (in Property Records' Historic Preservation tab, beside CRIS) — what the
  historic inventories say about the pin: the nationwide National Register plus 24 state SHPO and
  city/county registers, from REData's cultural-resources registry. Property Records' Overview names
  the place's own listing and its number; Location Data's Overview does not.
  Renders only REData's standardized fields (name, type, status, year built, style, use), so a
  register REData adds appears without a release; which registers cover the point comes from
  `GET /capabilities/`. New York's CRIS is excluded here — it has its own richer panel below
  (`plugins.builtin.redata_historic_registers`). A National Register row shows NPS's reference
  number linked to its NPGallery record and, where REData's row carries `attributes.NARA_URL`, a
  "National Archives record" link beside it (NPGallery shows an empty page, as a 200, for most listings
  after 2012; P256). Only `catalog.archives.gov/id/<number>` is accepted, rebuilt from the number, when the
  row is cached and again where it is shown (`national_register.nara_record_url`); a row cached before
  shows NPGallery alone until it is refetched. Fetching adds both links to the pin's and wiki's links
  (marked automatic) for each listing that is the place's own. On one building of a larger site,
  only that building's own records show: a structure listing whose own point stands on it, or the listing
  holding it when CRIS's record of the building calls it listed; the campus listing whose boundary
  merely holds a building does not (`services.locations.national_register`, P230)
- **NY Historic Preservation (CRIS)** (New York, in the Historic Preservation tab after the
  registers, under its own heading) — the nearest surveyed building's USN record
  (eligibility, address, USN number); on one building's own location, only a record whose point
  stands on that building (`national_register.stands_on`, P255); or the historic district/National Register listing on a
  parcel-scope pin (NYSHPO's own National Register number, and NPS's reference number linked when
  the Historic Registers rows name the same listing, unless those rows already show it in the same
  tab; CRIS has no public link to a record), plus that building's and site's survey photos and scanned forms in the Media
  gallery. A parcel-scope pin (a campus) also gathers every CRIS building inside the site record's
  footprint and any it links, each attachment tagged with the building it documents; REData is
  asked to warm the whole site with its bulk `fetch-details/`, and each pass live-fetches at most
  12 buildings REData has not detailed yet (`plugins.builtin.cris_buildings`, P24). That site
  fetch also answers the campus's building children, so a campus costs one site fetch rather than
  a REData round trip per building: the payload keeps a `campus_buildings` roster, and each pin or
  wiki nested under the site whose footprint holds a roster building's CRIS point (or, with no
  footprint, that stands within 15 m of one no other building on the parcel is nearer to) gets its `cris_building_usn` card written,
  dated as the site's row so it goes stale with it. Its media half is filled when the child is
  opened, from the same payload, with no REData call unless the site pass left that building
  undetailed (one detail fetch then), and that is when its documents are queued for extraction.
  A child opened before its site has an answer fetches the site once for every sibling, waiting
  on a site fetch already in flight. The sweep that creates building pins seeds them the same way
  (`external_data.seed_site_descendants`), and background enrichment takes a nested location's
  card from its site before looking it up. A child the roster does not cover fetches its own.
- **Wikimedia Commons** — archival photos/media, direct (REData has no equivalent provider);
  scanned books and reports (PDF, DjVu) go to Article > Sources rather than the gallery
- **Smithsonian Open Access**, **Library of Congress**, **Internet Archive** — archival photos/media, via REData;
  a PDF or DjVu among the results is listed under Article > Sources rather than the gallery, as for every REData archive
- **Historic Newspapers (Chronicling America)** — dated newspaper pages (1794-1963) about the
  place, in the Media gallery; USA only, via REData (`ChroniclingAmericaMediaProvider`). A page is shown when
  its OCR text names the place; its paper's dateline is not read (P216)
- **Aerial & Drone footage** — a Media-gallery tab of overhead views, from REData's pooled media
  index filtered with `is_aerial` (`plugins.builtin.redata_aerial_media`)
- **Nearby Media** — a Media-gallery tab of everything else REData's pooled media index holds near
  the pin (Commons, Flickr, YouTube, NPS media, ...), judged by the media relevance rule; images
  REData has mirrored are served from REData's copy through this site's proxy (`pin.redata.media`).
  It shares one REData read per point with the Aerial tab - `/locations/context/` when REData has
  the point cached, else `/media/lookup/` asked only of the providers these two tabs render - never
  the street-level networks, which the Street-level tab reads from `/street-view/` (a provider REData
  refuses as `unknown_provider` is retried once unfiltered, with a logged warning)
  (`services.apis.locations.redata_media_gateway.NEARBY_MEDIA_PROVIDERS`,
  `plugins.builtin.redata_nearby_media`, `services.locations.redata_point_data`). A row REData marks
  `attributes.mirror_gone` - its source answered that the image no longer exists - is left out of
  every REData media tab, the Street-level tab and the street-view carousel, before the shared answer
  is cached (P325)
- **Street-level** — a Media-gallery tab of dated street-level captures near the pin (Mapillary,
  KartaView, Panoramax via REData `/street-view/` and its timeline), one per network and date,
  each opened from REData's archived copy through this site's proxy (`pin.redata.street_view`).
  The satellite carousel's street-view slides read the same shared timeline
  (`plugins.builtin.redata_street_level`). A date's picture is its nearest frame whose image is not
  gone; a date with none is left out
- **Nearby Photos** — a Media-gallery tab of this site's own photos that REData's photo relevance
  index places near the pin or on its parcel (`/photos/lookup/`, `/parcels/{uuid}/photos/`), ranked
  by REData's score. Only the photo ids are cached; each viewer sees only the ones they could
  already see elsewhere - their own, or ones shared to a wiki they can reach - never a photo shown
  to them only through a message or check-in, and never one of this pin's own place. Its tiles offer
  no save, send-to-wiki or relevance copy - a member's photo stays where they shared it
  (`plugins.builtin.redata_photo_pool`, `GalleryMediaSource.for_viewer`, `shows_members_media`)
- **Digital Commonwealth** (Massachusetts) — photographs, maps, and documents from MA libraries/museums/archives, via REData; Massachusetts pins only
- **Media previews** — Media-gallery items in formats no browser renders (archival TIFFs, scanned
  PDF inventory/nomination forms, HEIC) are rasterized to JPEG/PNG server-side rather than left as
  a broken tile or an anonymous document icon (`services.media.previews`). A remote item is
  rendered into this site's copy of it (`services.media.remote_copies`); the in-app REData proxies
  (CRIS attachments, LoopNet photos) render their own via `?preview=1`, passing already-displayable
  files straight through
- **Web Images** — broad web-image search across many engines (Flickr, imgur, Pinterest,
  DeviantArt, Openverse, Unsplash, …) via REData's web-search image mode, using an aggressive
  relevance query (all non-nickname aliases · state/country + municipality · the site's
  urbex/abandoned subject vocabulary) so a same-named place or operating business elsewhere is
  excluded; a child pin (a building nested under a parent parcel/site pin) adds a required clause
  of the parent's own names too, since a generic building label ("Staff House") carries no
  identifying power alone (`plugins.builtin.searxng_images`). The public Flickr search and the
  general web-search panel apply the same parent-name qualifier for child pins
  (`services.apis.flickr.search`, `Pin.get_unique_search_name`)
- **National Park Service** (USA) — nearest park info, via REData; for a pin inside the park (or a
  `SiteFeature.PLACES` viewer) also its alerts, visitor centers, campgrounds, the park's places
  nearest the pin and links to its webcams. Each park facet is asked once per park and shared by
  every pin near it (`plugins.builtin.nps`)
- **Yelp** — nearby business details, via REData
- **LoopNet** (USA) — commercial real-estate listings
- **Property Records** (USA, the card's Parcel tab) — county parcel ownership/tax/sale-history lookup, retrieved from
  REData via `RedataGateway`
  (`services.apis.property_records.redata_gateway`); populates the Ownership and Sale History cards,
  on the wiki and on the Private Pin page alike, with `OFFICIAL`-sourced records in addition to a details
  card. REData's `/owners/` adds contact details, former owners and how many other parcels an owner
  holds; its `/sales/` adds sales an earlier retrieval saw. A location's linked owners are its current
  ones: a seller is kept on the sale only, and an official owner the newest record no longer names is
  unlinked. Coverage varies by county. **Owner names and contact details from those `OFFICIAL` records are subscriber-only**
  (`SiteFeature.PROPERTY_OWNERS`, enforced in `services.property.owner_access`) - the parcel, tax,
  assessment and district facts stay open to everyone, as do a user's own private `PinOwner` notes
  and any `WikiOwner` the community typed in themselves
- **USGS Historical Topo Maps** (USA) — historical topographic maps, direct-only (a gallery of
  individually-dated scans, a shape REData's imagery contract doesn't offer)
- **Historical Features** — retrospectively-mapped buildings, roads, water features, railways, land
  use, places and venues that once stood near the pin (mostly demolished, mostly never formally
  designated), via REData's `/historical-features/` (self-hosted OpenHistoricalMap/Overpass-backed,
  worldwide but volunteer-traced/city-scale coverage) (`plugins.builtin.redata_historical_features`).
  Distinct from Historic Registers (a body's own designation) and USGS Historical Topo Maps (a
  scanned page) — this is per-feature data with its own validity interval. `start_year` is
  frequently the date of the *source map* a feature was traced from, not a construction year, and
  the panel never presents it as an age
- **Nominatim/OpenStreetMap** — reverse geocoding and place metadata (two panels: Nominatim
  structured data, kept direct-only for its OSM extratags REData doesn't normalize; Photon
  nearest-feature lookup, via REData)
- **Panel placement** — an info panel declares where the Private Pin page shows it:
  `InfoPanelSource.placement` is `PanelPlacement.STANDALONE` (a card of its own, the default),
  `REGIONAL`, `LOCATION` or `PROPERTY` (a tab in one of the cards below), with `tab_label` and
  `tab_order`. A panel naming another in `shown_in` has no tab or card of its own: the other panel's
  tab (and building card) renders both, each under its own title, and drops a later panel's fact that
  repeats a link or a label and value an earlier one gave (`external_data.without_repeats`). Each keeps
  its own gate, fetch and polling. A plugin panel picks its card by declaration; the controller holds
  no list of keys (`services.pins.external_data.tabbed_panels`)
- **Tab bodies** — a tab whose data is still being fetched shows its spinner (the pending
  placeholder is hidden only as a card of its own), and one whose polling ends empty or fails says
  "No data available." or "This data is temporarily unavailable." rather than going blank
  (`shared/external-panel-fallbacks.ts`). A card collapsed when the page opened loads its open tab
  when it is restored (`shared/collapsible-sections.ts`)
- **Panels known to be empty are never requested** — while it renders, the Private Pin page works out
  which of its standalone info cards, bespoke cards (Azure Maps, Buildings, Yelp, NPS, LoopNet, USGS
  Topo, Wikipedia) and Media providers already have nothing for this pin, and renders no placeholder
  for those (`services.pins.panel_probe`, P53). "Nothing" means what the panel's own request would
  have answered 204 or an empty gallery on: its gate refuses the pin, the owner turned external
  services off, its fetch is suppressed after a failure (`external_data.fetch_blocked`), or the
  cached answer shows nothing (`LocationCachePanelSource.shows`, which is `render_context` for an
  info panel; `media_items` for a provider). A panel with no stored answer still loads and fetches.
  The decision reads one batch of cache rows, refuses every gateway request, and leaves a remote geo
  boundary (a state outline from TIGERweb) unresolved, counting such a panel as possibly having
  content. It covers only sources the viewer may see. A debug-overlay viewer still loads every Media
  provider, since an empty one reports what it searched for. Decided in the page's own request
  rather than in a separate one: no extra round trip before the panels start, no out-of-band swaps
  into the panels' scattered slots, and a probe that fails loads every panel as before
- **Regional Data** — data about the area rather than the site: US Census, Wildlife (iNaturalist),
  Seismic (USGS earthquakes), Disasters (Fire & Disaster History), Water (Water & Hydrology), Air
  Quality and EPA, each loaded when its tab is opened; the first tab with something to show opens by
  default (a cached empty answer is passed over), and a tab with nothing to show says "No data available."
- **Location Data** — data about this place: an Overview merging every tab's
  `overview_summary()` into one unattributed list, then Nominatim, Photon, Elevation and Site
  Conditions. Tabs that settle with nothing to show are removed, and so is a tab whose panel's gate
  refuses the pin, which the Overview never fetches (`PinController._card_overview`)
- **Property Records** — the parcel's and building's records: an Overview (owner, parcel number, the
  main building's year built, historic status and National Register number), then Parcel, Building Characteristics (not
  on a parcel, whose buildings carry their own) and Historic Preservation (Historic Registers and
  CRIS). The Overview names an official owner only to a viewer holding `SiteFeature.PROPERTY_OWNERS`,
  as the Parcel tab does, and says "Owner on record - subscribers only" to anyone else. It counts
  only the place's own records: a listing whose boundary holds it or whose point stands on it (for one
  building of a site, P230's rule), and a CRIS record standing on the building or, on a site, the site
  record holding it. It fetches its tabs' data and removes empty tabs, as Location Data's does
- **Building Characteristics** — Overture Maps' class, height, floor count and roof of the building at
  the pin, plus named places within 150 m. Where REData's Overture mirror holds the point - inside REData's US
  boxes and one of the padded state shards it syncs (`overture.served_by_redata`) - it reads REData
  (`/buildings/` and the `overture` points-of-interest provider), and an empty answer there is final.
  Elsewhere, including border cities in Canada and Mexico, the Bahamas and the western Aleutians, it reads
  Overture's public release (`plugins.builtin.overture_building_attributes`, `OvertureProvider`). An
  install without REData shows it only where REData's mirror does not reach. A building whose nearby
  places were not heard from is kept for an hour, not the cache window (P240). Only a footprint containing
  the pin counts, with no nearest-building fallback: a pin outside every footprint, such as a parcel's pin
  set on its grounds, stands for the parcel and gets no building data, which comes instead from the child
  pin made for each building (Buildings on this Property)
- **Buildings on this Property** — every structure standing on the parcel, with names and building
  numbers from REData (county GIS building-footprint layers plus NY SHPO CRIS), falling back to
  OpenStreetMap footprints inside the property boundary. Each row links to the sub pin covering
  that building at any depth - every record of one physical building links to the same pin - or
  offers to create the ones that have none (`plugins.builtin.parcel_buildings`). A row says when its
  building was built only from the building's own year, never the parcel's. On a pin's page it
  is also where child pins are listed: a Child pins tab has every direct child of any type, a child
  pin with children of its own gets that list alone, and the header adds a child pin or pulls the
  wiki's in. CRIS's campus buildings are in this list, not repeated on the CRIS tab.
  Also shown on the wiki page
- **News** — recent news coverage scoped to the location (appears for notable locations), via
  REData's GDELT-backed search
- **Cameras & Structures** — mapped surveillance cameras (individual agency registers plus
  OpenStreetMap's worldwide contributed set, which outside Chicago and Austin is the only camera
  source there is), FCC-registered antenna structures, FAA facilities, EPA contamination programmes
  and storage tanks near the pin, grouped by REData's own category label
  (`plugins.builtin.redata_site_features`). **Which sources are asked is discovered at request time
  from REData's `/capabilities/` index, not listed in UrbanLens** — most of these providers are
  generated on REData's side from dataset tables, so a register REData adds appears here with no
  UrbanLens release. Providers that already have their own panel (Yelp, EPA ECHO, NPS places) are
  excluded so nothing is shown twice
- **Underground Structures** — OSM-mapped tunnels, culverts, station levels, shafts and buried
  utility runs within 250 m, enterable features first, via REData (`plugins.builtin.redata_underground`)
- **Permits & Violations** (US cities) — the site's building-permit/code-violation/site-plan filing
  chronology with deep links to city records and plan drawings where published, via REData
  (`plugins.builtin.redata_permits`); flags when a dense block capped the result
- **Reported Incidents** (US cities) — block-scale police-incident reports from city open-data
  portals as visit-safety context, via REData (`plugins.builtin.redata_incidents`); traffic
  collisions excluded, block-scale location precision stated on the panel. **Incident History is
  subscriber-only** (`SiteFeature.INCIDENT_HISTORY`) — a deeper, separately-gated sibling panel
  pulling REData's full 25-year window as a year-by-year trend, instead of the free panel's last 3
  years/top 6 rows; the free panel is unaffected and stays free (see D10,
  `docs/designs/incident-history-feature-gate.md`, for why it isn't folded into
  `SiteFeature.NEARBY_RESEARCH`)
- **Water & Hydrology** (USA, a Regional Data tab) — streams, waterbodies, wetlands (USFWS NWI decoded) within 1 km and
  the containing HUC12 watershed, via REData (`plugins.builtin.redata_hydrology`)
- **Site Conditions** (USA, a Location Data tab; its panel is requested when the tab opens, and the card's Overview
  fetches its data in the background, as for the card's other tabs) — NLCD land cover, EPA
  walkability index (incl. transit distance), and USDA SSURGO soil composition (dominant-first, no
  invented averages) folded into one panel, via REData (`plugins.builtin.redata_site_conditions`)
- **Air Quality** (a Regional Data tab) — current modelled readings (Copernicus CAMS, worldwide) with a count — never an
  average — of nearby community sensors, and when the reading was taken; refreshed hourly, via REData
  (`plugins.builtin.redata_air_quality`)
- **Fire & Disaster History** (USA, the Regional Data "Disasters" tab) — NIFC wildfire perimeters that reached the site (back to
  ~1900) and FEMA disaster declarations for its county (since 1953, with which assistance
  programmes were authorised), via REData's hazards registry (`plugins.builtin.hazard_history`)
- The Property Records card also lists the parcel's **assessment history** (annual assessor
  valuations with their review stage — mailed/certified/board — Cook County today, via REData's
  `/parcels/{uuid}/assessments/`), and **supplementary recorded sales** (CT OPM, Cook County via
  `/parcels/{uuid}/sale-records/`) feed the Sale History cards — matched to the parcel by
  address/PIN before attribution, with non-arms-length transfers excluded (see
  `docs/designs/redata-integration.md`)
- **OpenWeatherMap** — weather forecast; appears on Trip detail pages (keyed to activity location) and on the Private Pin page when weather data is available. Via REData when configured, falling back to a direct OpenWeatherMap/Open-Meteo call
- **What the weather was** — past trip activities show the *recorded* conditions for their day
  (high/low, rainfall, snowfall, peak wind and gust) from REData's `/weather/history/` (ERA5, worldwide,
  back to 1940). The panel reads stored `RecordedWeatherDay` rows and queues the missing days, fetched
  in clustered ranges so activities decades apart never become one request for every day between them;
  while anything is still arriving it asks once more after a few seconds. Days inside ERA5's ~6-day
  publication lag are not requested, so they are never stored blank. Upcoming activities get a forecast
  only within `FORECAST_HORIZON` (`controllers/trip.py`), cached per place for an hour and fetched under
  `WeatherForecastUpstream`. Activity times are bounded to 1900-2199 by the trip services and a DB check
- **Sunrise/sunset & golden hour** — via REData when configured, falling back to direct Open-Meteo (its 5-day/3-hour OpenWeatherMap counterpart has no sunrise/sunset field), shown alongside the Private Pin page's weather panel; golden hour is approximated as the hour after sunrise / before sunset
- Satellite imagery carousel: Google Maps and Esri (incl. up to 5 historical Wayback releases) are
  direct; additional providers (NASA GIBS, Mapbox, Bing Maps, OpenAerialMap, OpenTopoMap) via REData
- Street-view carousel: Google Street View is direct; Mapillary, KartaView, and Panoramax are via
  REData's `/street-view/timeline/` - one dated slide per capture date (representative frame
  nearest the pin, newest first), so the carousel reads as a decay progression rather than an
  undated handful of recent frames
- Debug overlay (admin-only) to inspect raw external-API responses per panel
- **Searches built from names are cached per name set** (`services/pins/search_names.py`, P188) - web
  search, Web Images, Flickr, News and every name-searched Media provider (`NameSearchSource`). A
  Location's *shared* names are its official name and its wiki's name and non-nickname aliases; a
  pin's *custom* names are its own name and non-nickname aliases that restate no shared name, plus
  the names of the pins it is filed under. Each provider makes one search from shared names, cached
  for every viewer (`LocationCache.audience` `""`), and one per distinct custom-name set, cached under
  a digest of that set: two pins with the same set share it, nobody else reads it. A pin page, its
  Photos tab and the external API read the shared row plus their own set's row, combined; wiki pages
  read the shared row only. Custom names are searched casefolded and longest first, so a set's query
  is the same whoever holds it. Each row keeps the names that built it under `search_names`

All external integrations are cached (DB-backed, per-Location) and rate-limited per service, with
usage tracked in `ApiCallLog`/`ApiRateLimit` and toggled at `/site-admin/api-limits/`. A per-call
cost estimate (`ApiCallLog.cost_estimate`, from `ServiceDefaults.cost_per_call`) is logged for
services with a known published rate - `null` means "not priced," not "confirmed free," since
most services don't have a rate configured yet. Aggregated into a per-service 30-day cost
breakdown on the site-admin API usage report; the public `/costs/` transparency page (below) shows
a coarser blended figure instead, not a per-service breakdown. Calls that bypass a gateway session
(vision, and every LLM feature: article expansion/safety, trivia generation, moderation, answer check
and wiki incorporation, link extraction, document pin import, trip suggestions, label style and
category suggestions, and each assistant round) reserve their ledger row before calling through
`rate_limiter.api_call_slot()`, so their admin limits and enable switches apply, and a limiter that
cannot read its counts refuses billable services. The six LLM features added to it on 2026-10-05
carry no cap: AI is logged, not limited.

A service's `ServiceDefaults` reach its `ApiRateLimit` row when the row is created, and once more if the row still
holds the generic 20 a minute, 500 a day fallback when the service first registers defaults. An admin can edit every
field, so a changed default reaches existing rows only through a data migration that rewrites a row still holding an
earlier default exactly and logs one it leaves: 0064 moved 0.8.0's values to 0.9.0's, and 0068 does the same for a
default any release has written, read from git history (Overpass rows from v0.3.0b0 and v0.4.0b3 allowed 2 calls a
minute). `enabled` is never touched.

A service declared `ledger=CallLedger.TALLIED` (`basemap_vendor_tiles`, one call per uncached raster
tile) makes no query at all per call: it is checked against the same `ApiRateLimit` limits, share
and switch on atomic fixed-window counters in the default cache, and each outcome is added to a
per-service tally that the `api-call-tally-rollup` beat entry writes into `ApiCallLog` every minute
as rows carrying `calls` (`services/core/call_tally.py`). The row's limits reach each process the
same way, published to the cache by the roll-up and on every save of the row. The API-limits
summary, the costs page and provider health count `calls`, not rows. A counter store that cannot
answer refuses the call.

**Per-environment egress policy (D26, `UrbanLens/egress.py`, `services/core/egress.py`).** Every
external service is classified (`ServiceDefaults.category`: `redata`, `internal`, `quota`, `billed`,
`ai`, `messaging`, `public_write`), and the same choke point applies one policy per `UL_ENVIRONMENT`:
REData and our own hosts (Ollama included) everywhere; hosted AI (Cloudflare Workers AI, OpenAI, Anthropic) on
production and staging, refused in development and local, where each AI feature says "not available in this
environment"; `quota` and `billed` budgets scaled by `UL_ENVIRONMENT_SHARE` (production 0.9, staging 0.05,
development 0); messaging and writes to a third party (Save Page Now, Calendar, Stripe) production only.
`UL_ENVIRONMENT_SHARE_OVERRIDES` opts one service in or out, an AI feature or provider (`ai_cloudflare`) included. A
refusal (`EnvironmentRefusedError`) writes no ledger row, logs once per service per ten minutes, is
skipped by the boundary chain rather than deferred, shows as "Not available in this environment" on a
panel without caching anything, and off production the name and geocode chains never fall through
from REData to a direct provider. Beat schedules only internal entries off production (plus
`UL_BACKGROUND_TASKS_ALLOWLIST`), and each external task checks for itself. The hosted Protomaps
basemap tiles are fetched by the browser on production and staging, with the self-hosted mirror's as the
automatic fallback; development and local draw the mirror's. The startup log names the
environment, share, overrides and allow-listed tasks.

A request no provider could answer is refused before it spends anything: no rate-limit slot, no
quota, no network. `services/core/input_validation.py` raises `ImpossibleInputError`, a "no data,
don't retry" error that is not an outage. Every gateway session refuses a NaN or infinite number
anywhere in its parameters or body, and a named latitude or longitude off the globe. Each gateway
then refuses its own impossible inputs:

- a blank query;
- `(0, 0)` where the source has nothing at sea (weather, Wikipedia and satellite imagery still take it);
- a malformed Google place id or photo name, Nominatim OSM id, VirusTotal SHA-256 or Twilio number;
- a radius, limit or tile coordinate outside the provider's range;
- an empty AI prompt or image.

Each refusal writes one `ApiCallLog` row with `was_rejected_input`, which no budget window counts and
the API-usage report shows. It logs at INFO once per service and reason every ten minutes.
Callers treat it as an answer of nothing:

- panels store `{}` or skip;
- `request_upstream` answers 400;
- the tile proxy answers a 404;
- routing returns no route.

Beyond on-demand fetches, an hourly **background enrichment** task drips high-value lookups
(official names, aliases, street addresses, building boundaries) into whatever rate-limit budget
is left over after real traffic, spread evenly so multi-day quotas can't be burned in one day.
Sources are plugin-contributable (`EnrichmentSource`) and admin-tunable (run window, reserve
buffer, per-run caps). A new root pin does not wait for it: its property is bootstrapped when it
is created, and a location a bootstrap is filling is not a candidate, so the per-run cap goes to
backfill (imports, and pins from before bootstraps).

Neither path caches an outage. A failure `services/core/gateway.py::is_source_outage` recognises (connection failure,
timeout, 5xx, throttle, REData `source_error`, an empty envelope REData marks incomplete) writes no `LocationCache`
row, so the next view or batch asks again; a real empty answer is cached for `external_data_cache_days`.
`tests/hypothesis/test_outage_not_cached_registry.py` holds every registered panel and enrichment source to this.
A REData 503 is never a settled answer whatever its reason (REData answers 404 for a permanent one), and a parcel
lookup it could not settle is not asked again for 12 hours. A panel source whose upstream keeps its data for hours
declares `cache_max_age` (air quality 1 h, the NPS card's alerts 6 h), which shortens the site-wide window for that
source alone.

An adopted site answer keeps the site row's age. Near-point panels send a `limit` of at most 200 (REData answers 400
above that), keep no more than they asked for, and say "N+" or "more than shown" when an answer filled it.

**REData refusals.** A 401/403 from REData (a key lacking an endpoint's scope) opens that endpoint's breaker
(`services/core/upstream_breaker.py::RedataBreaker`) for an hour across every caller, is logged once at error level,
and is listed on the site admin's API limits page until a day passes without one. Both are per API key, so a rotated
`UL_REDATA_API_KEY` is asked at once.

**Without REData** (`UL_REDATA_API_URL` unset) every REData-only surface is unavailable rather than failing: panels
gate through `services/pins/redata_panel.py::RedataBackedSource`, and media, satellite and street-view providers
through `available()`. Nothing is scheduled and nothing is cached, so REData's first answer once configured is real.
`tests/hypothesis/test_redata_absent_surfaces.py` sweeps every registered panel and imagery provider for this.

## Extensibility: Plugin System

Third-party integrations are packaged as **plugins** (`dashboard/plugins/builtin/`) — see
`docs/designs/plugins.md` for the full contribution API. A plugin can add rate-limited services, Private Pin
panels, satellite/street-view providers, place-name providers, and lifecycle hooks. Plugins are
discoverable from bundled modules, an env-var module list, or pip entry points, and can be
enabled/disabled per-install or per-service without a restart. Inventory at `/site-admin/plugins/`.

## Vault (Photos, Documents & Albums)

- **Vault** is the top-level nav section for a user's personal media library - `/vault/`. It
  replaced the old Memories → Photos page (bookmarked `/memories/photos/*` links permanently
  redirect to their `/vault/photos/*` equivalents) and added a parallel Documents page, a
  personal (pin/wiki-independent) album space, and a landing page, none of which existed before.
- **Vault home** (`/vault/`) — quick-link tiles into Photos/Documents/Albums with live counts, a
  storage usage summary (used/quota/remaining, shared with the Settings → Storage section), and a
  recent-uploads strip mixing the most recently added photos, videos and documents, and a
  listed Videos section (videos have no page of their own - this is where they are reachable).
- **Vault → Photos** (`/vault/photos/`) — the site-wide photo library: matches unfiled photos (by
  GPS + timestamp) to existing pins and proposes **visit suggestions** for confirmation; an
  organize queue surfaces photos that still need a pin, a location, or suggestion review, plus
  pending upload failures and caption/GPS metadata conflicts to pick between. The grid is a
  windowed/virtualized infinite scroll (skeleton tiles while the first page loads, off-screen
  thumbnails pruned via IntersectionObserver as you scroll so memory stays bounded on a library of
  hundreds of photos) with a sort control (Recent / Oldest / Taken / Name).
- **Vault → Documents** (`/vault/documents/`) — a parallel page for non-photo files (PDF, Word,
  Excel, PowerPoint, plain text), sharing Photos' grid/skeleton/pruning/sort infrastructure with
  document-appropriate tiles (a type icon + filename) and a lightbox that swaps the image viewer
  for an inline `<iframe>` preview. Gated behind the `document_uploads` subscription feature/site
  default; the page itself always renders, just without the upload dropzone when the viewer lacks
  the feature.
- **Vault gallery kinds** — Photos and Documents are one set of views
  (`controllers/vault_media.py`, routed with a `kind` kwarg), one page template
  (`pages/vault/media_page.html`), and one client (`shared/vault-media-grid.ts`,
  `shared/vault-uploader.ts`), with what differs in `MEDIA_KIND_SPECS` (`models/images/kinds.py`).
  Another kind is a spec entry, its routes, a child template, a tile partial and a TS tile renderer.
- **Vault albums** — a personal, pin/wiki-independent album space (create/rename/delete, add/
  remove/reorder photos) using the same `Album`/`AlbumItem` infrastructure as pin/wiki albums
  below, with a toggle to also surface your existing pin albums from across all your pins
  alongside them. No owner-slug segment (one Vault per profile), no move-to-another-pin or
  floorplan-overlay actions, and no cover-hero banner - those stay pin/wiki-specific.
- **Lightbox associations** (Vault Photos/Documents) — the shared lightbox shows a photo's pin/
  wiki filing (or "unfiled") and its pin/wiki album memberships, plus actions to **file it to a
  pin** (autocomplete search, only offered while unfiled - an already-filed photo links to its pin
  instead of silently reassigning it), **send it to a wiki** (wiki search/picker, attaches the
  photo the same way the pin gallery's bulk "Send to wiki" action does - a photo can carry both a
  pin and a wiki simultaneously), and **share it with a friend** (a lightweight single-photo DM
  share, distinct from a whole-pin share - repeatable shares of an already-shared photo create a
  quota-exempt duplicate copy rather than reusing the original DM attachment).
- Photo galleries on pins and wikis: drag-drop upload, reordering, lightbox, EXIF/GPS extraction,
  checksum-based duplicate detection. On a private pin, the lightbox's more-actions menu offers
  **Use as floorplan overlay**, which adds the photo as a georeferenced blueprint and opens the
  floorplan editor to warp it.
- **Albums** (Photos tab on Private Pin and wiki): named groupings of a place's photos (plain,
  visit, area, timelapse). Photos default to newest-uploaded; dragging one
  freezes a custom order for that album (later uploads stay at the end). Date
  and name sorts follow live metadata, so a caption or EXIF edit does not
  rewrite anyone else's position. Photos are always drag-reorderable. The album list shows covers plus
  photos not in any album, and pages its album cards as you scroll the way the photo grids do. The
  add/move-to-album dialog loads its own pages of albums when opened and searches every album by
  name on the server. Drag photos (including a multi-selection) onto an album card to file
  them. Multi-select uses the shared floating bulk toolbar (add to album, and inside an album also
  set cover / move / remove). Clicking a photo opens the shared lightbox; in select mode, click
  selects instead. Right-click on an uploaded photo (albums, pin gallery, wiki gallery) offers
  open / download / add to album / set as album cover / remove / send to wiki / share with a friend
  / delete. Set-as-cover is also in the lightbox and the photo-options menu. An album can be moved
  between a parent pin and a child pin from album options. When "show child pin details" is on,
  the Photos tab lists albums and unfiled photos from descendant pins as well. Dropping a file
  that's already been uploaded into an album files the existing photo rather than erroring.
  Uploading the same bytes to a different pin of yours reuses the stored file (no second quota
  charge); caption/GPS conflicts surface on Vault → Photos to pick. Failed or broken uploads
  toast and can be retried from Vault → Photos. Removing a photo from an album updates the grid
  immediately. Small grid thumbnails and a tiny (~44px) WebP map-marker thumbnail are generated in
  the background after upload (and backfilled on a schedule for photos that predate that); album
  grids load further pages as you scroll, unloading off-screen bitmaps so large albums stay usable
  on a phone. Photo map markers can be right-clicked to hide a photo from the map without clearing
  its stored GPS; the lightbox offers "show this photo on the map" to put it back. A stored
  photo's filename is always an opaque, year-prefixed-when-known token (never the uploaded file's
  own name, which can encode anything from a capture timestamp to a location) - the true filename
  is kept only on the row (`Image.original_filename`, encrypted), and a date parsed from a
  camera-app filename convention (e.g. `PXL_20260709_...`) is tracked separately
  (`filename_taken_at`) from EXIF-confirmed `taken_at`. In `services/photos/albums.py`,
  `visible_album_items(album, viewer)` is an album's viewer-visible membership rows as an unevaluated
  queryset (page, count, or `.values("image_id")` as a subquery), and `describe_albums` /
  `describe_album` compute count, cover and date range in SQL, in a fixed number of queries.
  `Pin.objects.tree_root_id(pk)` finds a pin's root in one recursive query.
- The lightbox lets you browse, search, and create+apply a media label in one step.

## Memories

- **Memories** page — aggregated timeline/map view of routes, trips, visits, and photos, including
  an "on this day" retrospective and a prompt to log visits for pins already marked visited; tabs
  for Timeline, Maps, Sharing, Journal, Visits (hidden when there's nothing unlogged), and
  Locations (hidden when there are no pending pin suggestions); date range filter with presets
  (Last 90 days / Last year / All time); "Import routes & history" for importing GPS tracks and
  location history (the map's import wizard, with its own copy). Its own Photos tab moved to
  **Vault → Photos** (see above).
- **Pin suggestions** — batch photo-location ingestion (a client-side local-folder scanner on
  the Tools page, or a full Immich library sweep) matches photo GPS against existing pins and
  clusters the rest into suggested new pins, reviewed on a multi-select map with bulk accept,
  pagination, and opt-in photo import. Bulk accept and the Visits tab's bulk unlogged-visit
  logging both run each row under its own savepoint via `services.core.bulk_outcome.run_each`,
  so a row that crashes rolls back only its own writes, and report failed rows apart from merely
  skipped ones through the shared `reportBulkOutcome` frontend helper
  (`frontend/ts/shared/pin-select-map.ts`)
- Storage quota accounting per user (role-based), automatic downscaling/WebP conversion on upload
- **HEIC/HEIF uploads are accepted and re-encoded to a format browsers render** (JPEG, or WebP when
  the uploader's policy asks for it). The transcode is not part of the downscale policy: it runs even
  for a user with downscaling and WebP conversion both off, because the stored bytes are what a plain
  `<img src>` gets and only Safari renders HEIC. GPS is still stripped or kept per the user's own
  setting, independently

## Trips

- Multi-stop trip planning shared among friends: activities, scheduling, map view
- RSVP per member with trip-wide defaults and per-activity overrides; per-activity thumbs up/down voting on proposed activities
- Trip comments with emoji reactions
- List and calendar views of trips, sortable. The list pages (`TRIP_LIST_PAGE_SIZE`) with ordering,
  including "soonest first", done in SQL; the calendar is rendered one month at a time on the
  server (`services/trips/trip_calendar.py`, `trips.calendar.month`). `TripQuerySet` has
  `with_effective_dates()` (subquery columns), `with_timeline_status()`, `timeline_counts()`,
  `overlapping(start, end)` and `search_for_member(profile, q, limit)`, which backs the capped trip
  picker (`trips.picker`)
- Two-way Google Calendar sync — connect an account, import calendar events as trips
  (attendees become friend invites), export trip activities to Calendar. The import dialog lists up
  to `MAX_IMPORTABLE_EVENTS` (500) events of the coming year, following Google's pages and saying
  when more exist; the import runs in the `import_calendar_events` task behind a progress poll, and skips
  an event once the profile is at `max_upcoming_trips_per_user`, saying how many were skipped.
  An export rewrites only events whose body changed (`TripCalendarLink.event_fingerprint`), creates
  under a deterministic event id so a retried create cannot duplicate, and when the calendar budget
  (ours, or Google's rate limit: a 429, or a 403 with a usage-limit reason) runs out partway reports
  "N of M" and leaves the rest to `requeue_pending_calendar_pushes`. Stops withheld from the
  exporter are written first, so a budget cut leaves the events that matter least. An event Google
  refuses for itself (`forbiddenForNonOrganizer`, a 400) is skipped and counted ("Google Calendar
  refused to change K"), and the rest are still written; a push that met one counts an attempt only
  when it wrote nothing else (UrbanLens#332). Only Google's refusal of the
  user's grant drops the connection: `invalid_grant` from the token endpoint, a 401 a fresh access
  token does not cure (the gateway refreshes and retries once), or a 403 naming a reason that is not
  a rate limit, not about one event (`forbiddenForNonOrganizer`) and not about the site. A refresh
  that gets a 5xx, a 429 or no answer is "busy" like a rate limit; a refusal of the site's Google
  project (`accessNotConfigured`, `SERVICE_DISABLED`, ...) or OAuth client, or a 403 naming no
  reason, is logged at ERROR and reported as calendar sync being unavailable (UrbanLens#302). A push
  or queued delete held up by any of those, by Google failing (`CalendarServerError`), or by our own
  limiter (unreadable, or the service switched off), waits for
  the sweep and is not counted toward `MAX_CALENDAR_PUSH_ATTEMPTS`; only a refusal of the write
  itself is, and anything still owed after `MAX_OWED_CALENDAR_WRITE_AGE` (30 days) is dropped.
  Each member's export applies their own location
  visibility: a stop whose location they may not see is exported with `location: ""` and the title
  the activities panel shows them (a title its author typed, else "Secret Location"), so an update
  clears a location an earlier export wrote (a PATCH keeps omitted fields). An imported event keeps
  its own location unless one is withheld. A change to what a member may see that does not save the
  trip (a trip-mate's `trip_pin_location_visibility`, an ended friendship, a removed pin, a deleted
  adder account) queues the same auto-sync push an edit does (`models/calendar_sync/signals.py`). A
  deleted stop or trip has the events UrbanLens made for it deleted from every exporter's calendar,
  through the calendar budget (`CalendarEventDeletion`, `delete_orphaned_calendar_events`, retried
  by the push sweep). An event an import linked from the user's own calendar is never deleted, on
  any path: "Remove from Google Calendar" and its API unlink it and say so, an export unlinks it
  when its stop loses its schedule, and `queue_calendar_event_deletion` refuses it (UrbanLens#301,
  UrbanLens#330). `manage.py clear_withheld_calendar_locations` (dry-run unless `--apply`)
  rewrites, once, the events of exports without auto-sync that may still hold a location or title
  now withheld, an unscheduled stop's included; an imported event only when its fingerprint shows
  UrbanLens wrote what is now withheld, and then only its location and title, each only where the
  event still holds what UrbanLens wrote; otherwise it is counted and left (UrbanLens#333)
- A hidden stop (its own "hide location", or its adder's `trip_pin_location_visibility`) shows a
  member who may not see it neither its place's name nor its location: the activities panel and its
  edit dialog, the external API (`title`, `effective_title`), the calendar export, the weather panel,
  `@act` mentions, visit suggestions on completion, the Memories timeline and global search all mask
  it. A title taken from a place search or an imported event's location
  (`TripActivity.title_from_place`) is masked with the location, and an imported stop whose only
  place is that title is withheld by its adder's setting as a located one is; a title the author
  typed is still shown (UrbanLens#303). An editor who may not see the stop who saves it with the
  title or place left blank
  keeps the stored ones; a place they pick replaces the location and drops a title taken from the
  old one
- Trip settings controlling member/organizer permissions
- **Invite by email** from the create dialog or the Add Member dialog (and `trips/<slug>/invitations/`
  in the external API). The inviter sees the address listed as invited whether or not it has an
  account; delivery runs in a Celery task so latency tells nothing, and the email budget is charged
  either way. An account that proved it owns the address (`find_verified_user_by_email`) gets an
  in-app notification; anything else gets the email. The invitee's page (`/trips/invitations/<token>/`)
  asks "join the trip?" and "become friends?" separately; signing up answers neither. Only the account
  that verified the invited address can answer, so a forwarded link is useless; someone who would rather
  not simply ignores the email. The creator sees and can withdraw every open invitation; a
  member's lapse on removal, or when they lose the right to add people (`services/trips/trip_invitations.py`)

## Safety Check-ins

- "I didn't come home" style safety net: create a check-in with expected return time and
  emergency contacts (registered friends or external email contacts). A contact added by email is shown as
  the address typed, never matched to an account for the owner; the account that verified it still gets
  the in-app alerts (`_contact_account` in `services/visits/safety.py`) and, once alerted, sees the check-in
  under "Shared with you" (`SafetyCheckinContact.objects.reaching`, matched on `email_normalized`)
- Escalation on missed check-in: emails emergency contacts, optionally posts to the location's
  community wiki, notifies pin owners
- The owner gets a "check in now" final warning about five minutes before escalation, and escalation waits
  for it: a warning sent late (a missed beat tick) holds escalation `FINAL_WARNING_MIN_NOTICE` after it, and
  one that never goes out holds it at most `FINAL_WARNING_MAX_WAIT` past the overdue point. Each side claims
  its row with a conditional write, so the warning never follows a contact alert (`due_for_final_warning`,
  `due_for_escalation`)
- A contact learns nothing of a check-in until escalation alerts them (GOALS.md, "Safety check-ins"); seeing it
  earlier is for an accepted partner, whom the owner chose. "Shared with you", the shared status page, its
  photos, and every magic-link token route (portal, photo, route map, chat, mark-safe, opt-out) reach only a
  contact with `notified_at` set (`SafetyCheckinContact.objects.alerted`, `by_token`). The "found safe" and
  "plan updated" notices go to those contacts only, so a partner resolving it early tells no one else
- When an escalated check-in ends, every alerted contact is told, once (`resolution_notified_at` is claimed
  before sending), through the alert's channels and behind the opt-out gate: "found" when someone reported the
  owner safe, otherwise an all-clear ("you can stop looking") for the owner checking in late, cancelling or
  deleting it. A contact alerted while the owner was checking in gets it from the escalation itself
  (`_tell_alerted_contacts_it_is_over`). A deletion resolves the check-in as `removed by owner` before deleting
  it, so that catch, which knows nothing of the deletion, still says it was removed rather than checked in
- A notice that fails is retried. One that fails to build releases its claim, and one whose email the worker
  could not send is marked (`resolution_email_failed_at`), so the escalation sweep sends it again, the email alone
  if the in-app half already went out (`retry_resolution_notices`). The sweep retries only between two minutes and
  an hour after the resolution: the request that resolved it finishes first, and after the hour the check-in is
  archived. It sweeps only check-ins this site resolved, never an imported one: every resolution schedules
  archival right after its claim, and an import never does. A deleted check-in leaves nothing to sweep, so
  deleting one first sends any notice its resolution still owes, an email that fails to build there goes out as
  plain text, and one that fails to send is re-queued by its own task, five times over about half an hour
  (`send_resolution_email`)
- Public (tokenized, no-login) contact portal for emergency contacts to mark the user safe,
  view attached maps, and chat in real time
- Live two-way WebSocket chat between check-in owner and emergency contacts
- Reusable saved emergency contacts, per-contact opt-out, auto-delete retention policy
- An opt-out holds however the person is added. One made as an account, or from an emailed link to any address
  that account verified (compared normalized), stops both the email and the in-app alert, as the opt-out page
  promises. One on an address no account has verified stops mail to that address only, so an account that
  never proved it is that person is still alerted in-app (`is_contact_opted_out`, `_contact_recipients`)
- An opt-out still works from a link clicked after the check-in is archived. Archival keeps a keyed hash of each
  alerted typed-in contact's address (`contact_address_digest`), the opt-out records that hash, and the gate
  matches it against every address a later contact is reached at, so it holds for any spelling, or for the account
  that verified the address. A check-in archived before the hash existed cannot record one, and the page says so
- Community-wiki posting is gated by `services.visits.safety.find_visible_community_wiki` and
  `community_wiki_opt_in`, so a check-in can only notify or link a wiki its owner can actually see

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
- `client_session_uuid` is an idempotency key (unique when non-blank): a retried upload gets the
  original upload back and is neither stored nor queued twice.
- **Individual scans are never retrievable through any API** — only the cumulative, unattributed
  marker per (device, wiki) is ever readable, and only for wikis the caller has already discovered.
- No manual marker-placement UI yet; markers are maintained entirely by the background pipeline.

## Social Layer

- Friendships: request/accept/reject/ignore/remove/block/mute, invite by email. Whether a request may be
  sent is `may_send_friend_request` (`services/social/friendship.py`), shared by the web view and the
  external API; every refusal (self, community off, a block, the target's `friend_request_visibility`)
  gives one generic answer, so none reveals which applied.
- **Invite by email** (friends page, external API, and tagging a visit participant with an address).
  The sender's pending entry looks the same whether or not the address has an account. An account that
  proved it owns the address is asked in-app; anything else gets the email. The invitee accepts or
  declines on `/dashboard/friendship/invitations/<token>/`, signed in as the account that verified the
  address (`services/social/friend_invitations.py`)
- **Mute is per-person and actually silences** — one column per side of the shared relationship
  row, so muting someone does not mute you to them. It suppresses the in-app notification and
  everything that hangs off it (live toast, WhatsApp/SMS, native push) for every notification type
  except the safety check-in family, which is exempt on purpose. Applied in one place
  (`NotificationLog.objects.notify`) and enforced by `bin/check_notification_choke_point.py`
- Configurable friend-request visibility ("anyone", "friends of friends", "anything in common", etc.)
- Public/friends-scoped profile pages with visibility controls per field (9 controls, each with 7 granularity levels from "Anyone" to "No one"), "view my profile as..." preview mode
- **A hidden account answers as a missing one.** Every route addressed by a profile's slug (the profile page and its
  panels, achievements, direct messages and their shares, e2ee partner keys, and the external API's profile, notes,
  social and messaging routes, and the direct-message socket's send, typing and open-thread frames) asks who may see
  the account in the query that finds it: `Profile.viewable_q`
  (`can_view_profile`) through `Profile.visible_by_slug`/`visible_by_identifier`, and `Profile.reachable_partner_q`
  (`conversation_reachable`) through `reachable_partner_by_slug`. A username nobody has and an account the requester
  may not see 404 after the same statements (P269). `test_profile_slug_side_channel.py` walks every such route (the
  socket's frames by hand, since no URL pattern names them) and holds both queries equal to their Python rules across
  each visibility setting and relationship. A profile named in a request body is resolved the same way (P280). Group
  members resolve through `accepts_messages_from_q`, invite usernames through `find_profile_by_username` with
  `invitable_q`, and share recipients, game invitees, group removals and game kicks through the sender's connections,
  the group or the session. `test_request_body_profile_side_channel.py` holds 21 such routes to the same status, body and
  statements as a name or id nobody holds. The friendship controller's id-addressed routes resolve through
  `Profile.known_to_q` and `friend_requestable_q` (P281, `test_friendship_id_side_channel.py`)
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

A single unified `Label` model (with a `kind`) backs five distinct UI concepts:

- **Tags** — freeform labels on pins/wikis
- **Categories** — hierarchical classification of pins/wikis
- **Statuses** — workflow state labels
- **People labels** — private labels a user applies to other profiles
- **Media labels** — labels on photos/videos/documents, for site search (own "Organize" tab,
  `LabelImageMembershipView`)

Shared features across all five: create/edit/delete, hierarchical parent/child relationships,
generic bulk edit, per-user color/icon customization (`LabelCustomization`, genuinely per-profile),
and a unified "Organize" management page. Two features are deliberately narrower than that: **bulk
convert between kinds** and **drag-to-reorder priority** are restricted to Tags/Categories/Statuses
- People and Media labels are server-side blocked from both (`_ORGANIZE_KINDS` in
`controllers/labels.py`; converting a People label via a crafted request is explicitly guarded
against, not just hidden in the UI). Multi-item **merge** does work for all five kinds; the
single-item merge UI is just hidden for People/Media.

Not every label-picking surface shares one implementation. The map filter sidebar, saved-filter
dialog, and bulk-edit dialog's label sections share the `createFilterPicker`/`createChipPicker`
factories in `ts/shared/label-picker.ts` and its search + kind-tab filtering UX. The quick-add-pin
dialog, the label merge target picker, add-labels-to-pin/location/image, and the organize page's
parent/child picker are separate, bespoke implementations that predate those factories - one of
them (the organize page's picker) doesn't even share the `@mixin tad-tabs` styling, by its own
code comment ("underline style, not pill buttons").

**Create-by-name is centralized.** Every write path that creates a label by name goes through
`Label.objects.resolve_or_create` (reuse the profile's own label, then a global one, before
creating) or `Label.objects.create_unique` (refuse with `LabelNameConflictError` instead of
reusing), both built on `Label.objects.named` - the `(lower(name), profile, kind)` lookup, own
labels before global (own only, for the profile-scoped category and status kinds). Both create inside their own savepoint and re-read on a raced
`IntegrityError` rather than trusting the first miss. `bin/check_canonical_creates.py` (pre-commit
manual hook `canonical-creates`, also run in CI) statically refuses a hand-written
`Label.objects.create/get_or_create/update_or_create` outside `tests/`/`migrations/`, so a new call
site cannot reintroduce the raw-lookup mismatch these replaced.

## External Category/Tag Data

`PlaceExternalTag` (`models/place/external_tag.py`) captures raw classification data external
providers report about a `Place` - **strictly separate** from the `Label` system above; a Label
is a user-curated organizational concept (including its own `kind=category`), this is unedited
provider vocabulary. Currently captured, per `Place`, with zero dedicated API calls (both piggyback
on data panels already fetch for their own purposes):

- **OpenStreetMap** — via the existing Nominatim reverse-geocode panel/enrichment source: its
  primary `category`/`type` tag pair (e.g. `amenity`/`restaurant`), plus `building`/`amenity`/
  `tourism`/`historic` when they add information beyond that pair.
- **Overture Maps** — via the existing "Building Characteristics" panel: a building's `subtype`
  and `class_`. Deliberately excludes that panel's "nearby places" data, which describes other
  points of interest near the coordinate, not the building itself.

Stored per-`Place` (not per-`Location`/pin) so every `Location` resolving onto the same building
shares one tag set rather than each re-deriving and storing its own near-duplicate copy; a
`Location` with no resolved `Place` yet simply isn't captured until one resolves.
`PlaceExternalTag.is_fresh_for()` further skips re-deriving tags a nearby Location already synced
recently. Shown read-only on the wiki page as chips visually matching the Label chip style
(`_external_tag_chips.html`, reusing the `.tag-chip` CSS with no edit/remove affordance).

**Tag equivalence mapping.** Different providers often describe the same real-world concept
differently (OSM `amenity=restaurant` vs. Overture `building_subtype=restaurant`), which would
otherwise show as two near-duplicate chips. `ExternalTagVocabularyEntry`
(`models/place/external_tag_group.py`) is a persisted, admin-curated record of every distinct
`(source, key, value)` tag ever seen - auto-registered as new tags appear, never auto-deleted -
which an admin can place into an `ExternalTagGroup` and mark one member `is_preferred`. With no
explicit group, two entries are still treated as equivalent by default whenever they humanize to
the same display text (`services.locations.external_tag_groups.default_group_key`) - an admin can
override a coincidental default match by putting the colliding entries into their own separate
(even single-member) explicit groups, since an explicit group always wins over the default.
`visible_tags_for_place()` resolves, for one Place's actual tags, which single tag to show per
equivalence group (the wiki chips only ever render this deduplicated set, via the
`visible_external_tags` template filter) - fed by the admin page at **Site Admin → Tag Mapping**
(`/site-admin/external-tags/`), which lists every known tag, surfaces unconfirmed default matches
for review, and supports fast bulk grouping (click/shift-click multi-select, or drag-and-drop
between groups via Sortable.js) rather than a dialog per pair.

**Search** — global search (pins and wikis) and the map's "Jump To" pin search both match by
external tag, including any provider's equivalent tag once admin-grouped or default-matched by
display text (`services.locations.external_tag_groups.matching_vocabulary`/`tag_match_q`) - see
"Search & Navigation" above.

**Not yet built, by design**: automatically suggesting Labels from this data, choosing pin icons
from it, or matching a tag *category* against its members ("Food and Beverage" finding a place
tagged "Restaurant") - neither OSM nor Overture data as ingested today carries that kind of
parent/child relationship, so there's nothing yet to curate it from.

## Notifications

- In-app notification center (bell dropdown), mark read/unread, per-type delivery preferences
- Actionable notifications (visit suggestions, friend requests, pin shares) leave the bell
  inbox when answered - with a short removal transition when acted on from the dropdown -
  and remain available on **Notifications → View all** (`/notifications/`) as history
- Real-time push over WebSockets (`ws/notifications/`) with desktop `Notification` API support and
  a 60s polling fallback
- Outbound email notifications with per-role rate caps (hourly/daily/monthly) and safety controls. `notification_delivery.send_notification_email` queues `send_notification_email_task` on commit, so no request waits on SMTP; `send_notification_email_now` is the send itself
- **Per-type delivery** (`services/notifications/notification_delivery.py`): `delivery_preference(profile, field)`
  reads one `DeliveryPreference` (SITE when the profile has no preferences row), and
  `deliver_notification(recipient, preference, title=, message=, url=, **log_fields)` writes the in-app row through
  `NotificationLog.objects.notify` (so mute applies) for SITE/BOTH and queues `send_notification_email` for
  EMAIL/BOTH, returning the row. New notification producers use these instead of branching on the preference.
- **Trip Updated / Community Wiki Updated** (`services/notifications/change_notifications.py`): a trip's joined
  members hear when another member changes its details, settings or activities; the people with a root pin at a
  wiki's place hear when someone changes it - any `WikiEdit` with an editor (fields, names, links, map drawings,
  markers, outlines, floor plans, reverts; `models/wiki_edit/signals.py`), an article save, an owner or sale. Fan-out
  runs on the bulk queue after the change commits. A burst folds: while a recipient's notification of one actor's
  changes to one trip or wiki is unread and was last added to within 30 minutes, the next change raises its
  `fold_count` ("made 3 changes") instead of writing a row, so the toast, push, text and email fire once. The actor,
  blocked pairs and muters hear nothing; wiki concealment decides per recipient whether they hear of a change at
  all. Automated writes (no editor) and photo, album and comment activity announce nothing.
- **11-event × 4-channel notification matrix** (Settings → Account): each event type (new message, friend request, check-in alert, AI task completion, etc.) can be independently configured for in-app, email, WhatsApp, and SMS delivery. WhatsApp/SMS require a phone number on the profile. WhatsApp/SMS delivery is wired for every event type: DMs and safety check-ins keep their dedicated pipelines, and all other types dispatch centrally via a `NotificationLog` post_save signal (`services/notifications/notification_text_alerts.py`) — delayed 2 minutes, skipped if read in the meantime, debounced per type per 6h.
- **Native-app push** (`models/push_device`, `services/notifications/push.py`): a backgrounded app
  holds no WebSocket, so it registers a push destination instead. **UnifiedPush** — an app-chosen,
  self-hostable push server such as ntfy — is the default transport, matching the project's
  self-hosted ethos and keeping an F-Droid build free of Play Services. An FCM row kind exists for
  a future Play-Store flavour and is deliberately not dispatched yet. Each task delivers at most
  `PUSH_BATCH_SIZE` devices, `PUSH_CONCURRENCY` at a time, and hands the rest to `dispatch_push_to_devices`
- Admin-only critical alerting via email + Gotify push (distinct from user-facing notifications),
  routed per event in SiteSettings: pin import errors, safety check-in archival failures, and
  uploads stuck waiting for storage (`services/media/upload_retry.py`)

## Custom Fields

User-defined private fields for **pins**, **photos**, **people**, and **maps**. Power-user feature for tracking non-standard attributes (e.g. access status, personal reference IDs, condition notes). Managed in Settings → Advanced. Text values are capped at
`services.core.text_limits.MAX_CUSTOM_FIELD_TEXT_LENGTH`; a link-typed value is validated against
`services.security.link_urls.clean_link_url`, the one http(s)-only rule every stored link (custom
fields, pin/wiki links, the archive importer, and the three external-API serializers via
`external_api.fields.LinkUrlField`) is checked against before it can end up in an `href`.

## External Photo Integrations

- **Immich** — connect a self-hosted Immich instance (server URL + API key) to browse and import nearby photos linked to pins
- **Google Photos** — OAuth import from a connected Google Photos library
- **Flickr (personal library)** — connect your own Flickr account (OAuth1) in Settings, then search/import your own photos on a pin's Media tab (near this pin, on recorded visit dates, or all)
- **Flickr (public album import)** — pin and wiki Media: paste the public URL of *any* Flickr user's album/photoset (no OAuth needed) to preview and import up to 100 of its photos, with the same confirm-grid + progress-bar workflow as the other importers
- All four importers share `services/photos/library_import.PhotoImport`: when media storage refuses a photo, the task is retried later for that photo and the ones after it (1, 2, 4, 8, then 15 minutes apart; a photo stored in between starts the wait over), so nothing already stored is downloaded or stored again. The progress dialog says storage is unavailable while it waits, and the closing toast counts any photos left when storage stayed down. A photo the source could not send (a network error included) counts as failed and the import goes on

## Account & Auth

- Email/password signup with verification, plus Google and Discord OAuth (social-auth pipeline)
- Password reset (themed to match the app, not bare Django pages)
- **A password change ends the delegated access that predates it.**
  `revoke_credentials_on_password_change` (`services/auth/credential_revocation.py`) is called by
  every path that sets a password - email reset, settings change, an SSO account's first password,
  Django admin, and E2EE enrollment - with a `PasswordChangeKind`. It re-signs the requester's own
  session, deletes every OAuth2 token, ID token and authorization/device code, and revokes API keys
  only when the owner (or admin) ticks the box the reset page, the settings form and the admin form
  all offer. `REENCODE` (enrollment re-deriving the same password) revokes nothing. Other browser
  sessions end by Django's password-hash check; open WebSockets follow within
  `_CREDENTIAL_REVALIDATION_INTERVAL_SECONDS` (session sockets close with the transient
  `CREDENTIALS_CHANGED_CLOSE_CODE`, so the tab that made the change reconnects).
  `bin/check_password_change_revokes.py` (pre-commit) fails a `set_password` call that skips it.
- **An SSO sign-in cannot claim an address it has not proved.** `resolve_sso_email`
  (`services/social_auth/pipeline.py`, before `create_user`) lets a provider address become the
  primary only when the provider verified it and `address_holder` finds no other account; an
  unverified one is left off and claimed through `email_claims.claim_address` by
  `claim_unverified_sso_email`, the same whether or not someone holds it; a verified address
  another account holds refuses the sign-in (only its owner can see that). `email` is in
  `SOCIAL_AUTH_PROTECTED_USER_FIELDS`, so a provider never rewrites it. `Profile.verified_primary_email`
  is unique (migration 0068); `email_claims.mark_primary_verified` is its one writer, and the
  User post_save signal clears it when the primary moves.
- **Passkeys** (Face ID, Windows Hello, security keys, Bitwarden-compatible) and **TOTP 2FA** (Google Authenticator, Authy, Bitwarden TOTP); backup codes available once passkey or TOTP is configured
- OAuth accounts can set a password separately to enable new-device encryption unlock without the recovery key
- Self-service account deletion (request with grace period, cancel)
- First-run setup wizard and a first-login onboarding tour with feature opt-outs; contextual
  in-product help tooltips on first visit to key sections (e.g. trip permissions, itinerary),
  with "Don't show again" opt-out per tooltip
- Login lockout after repeated failed attempts
- Signup verification mail is capped per address inside the deferred task (`signup.charge_verification_mail`,
  on the pending account's `EmailSendLog` ledger: one per `VERIFICATION_RESEND_COOLDOWN_SECONDS`, within
  its email budget), so rotating IPs cannot mail-bomb a pending address and the response never changes.
  Whether a resend should rotate the live token at all (G2-31) is undecided.
- **Any spelling of a username or address names the account.** Addresses fold by
  `normalize_email` (`services/auth/email_normalization.py`): lowercase everywhere; for
  `gmail.com`/`googlemail.com`, dots and a `+tag` are dropped and the domain becomes `gmail.com`.
  Other domains keep dots and tags. Usernames fold by `normalize_username_key`
  (`services/auth/username.py`): NFKC casefold, every non-alphanumeric dropped, look-alike digits
  mapped (`foo.bar`, `_f-o-o-b-a-r-`, `F00_Bar` → `foobar`'s key), stored as the indexed
  `Profile.username_key`. `find_user_by_identifier` (`services/auth/identity.py`) is the one
  resolver behind the auth backend, E2EE login-params, lockout keys, password reset, and admin
  grants; `find_user_by_username`/`username_search_q` back trip and check-in invites by username
  and the DM/group/global-search pickers. Email invites (friend, trip, visit tag) already matched
  through the normalized forms and verified secondaries. Registration refuses any spelling of a
  taken username with the same "isn't available" it gives a malformed or reserved one (P149); a taken
  address creates no account (P147). A key two legacy accounts share
  resolves to neither (only the exact username works); `googlemail.com` hashes stored before
  migration 0062 on `ExternalVisitParticipant` and `EmailSendLog` cannot be recomputed.
- **Outbound-mail guard** (`services/security/mail_guard.py`, `EMAIL_BACKEND`): every message
  passes `RecipientGuardEmailBackend`, which drops recipients no mailbox can exist at - reserved
  domains, and Gmail names holding characters Gmail never issues - and hands the rest to
  `UL_EMAIL_BACKEND`. A message left with nobody raises `SMTPRecipientsRefused`, as a relay would.
  Off production the delivering backend is Django's console one, so codes and magic links are read
  from the logs, unless `UL_EMAIL_SEND_OUTSIDE_PRODUCTION=true` (D26).
- **Enforced Content-Security-Policy** (`settings/base.py` `_CSP_DIRECTIVES`; `UL_CSP_ENFORCE=false`
  is an escape hatch to report-only). Violations are logged through `report-uri /csp-report/`.
  htmx features that need `'unsafe-eval'` are replaced by declarative request actions
  (`data-ul-on-success`, `data-ul-after-request`, `data-ul-before-request`; see
  `frontend/ts/shared/htmx-actions.ts`), lazy sections (`data-ul-lazy-section`) and
  `data-ul-min-query`. The Playwright page guard fails a spec on any violation. See N28.
- **External API keys** (Settings → Security → API keys): create/revoke/view API keys that let a
  third-party application act on the user's behalf with a scoped grant, drawn from a ~30-value
  `ApiKeyScope` vocabulary (pins, photos, wikis, trips, messaging, friends, notifications, safety
  check-ins, lists, labels, custom fields, undo, panels, assistant, games, search, device scans,
  and more) covering 100+ endpoints under `dashboard/external_api/` - grown well past this
  feature's original "read uuid, create pins" scope as mobile-app parity work landed. A freshly
  issued personal-access-token key's default grant (`PROFILE_READ`, `PINS_READ`, `PINS_WRITE`,
  `PUSH_MANAGE`) already covers reading/patching/deleting the owner's own pins, not just creating
  one. OAuth2 clients (PKCE) can additionally be user-consented into scopes a bare PAT key cannot
  hold (e.g. `messages:*`) - see `OAUTH2_ONLY_SCOPES`. Keys are hashed (never stored in plaintext,
  like backup codes) and revocation takes effect immediately. See `dashboard/external_api/` and
  the REST API section above.

## Undo / Data Safety

- Generic undo framework: deleting a pin, wiki, trip, safety check-in, saved filter, pin list,
  label, or markup map stashes a durable snapshot (on the ``UndoAction`` row itself, not in a
  cache) restorable for a retention window. Restores pre-check the constraints the recreate
  could violate and refuse cleanly rather than 500ing; relational pieces that were never part
  of the deletion (a list's member pins, a label's parents, a map's annotation authors)
  restore leniently, skipping whatever has since been deleted. Mutations (moving a pin,
  adding/removing labels or aliases, album membership, photo map position/metadata) stash a
  before/after payload on the same stack. Undo stamps the row rather than deleting it, so
  redo is a second pass over the same entry; a new action discards the redo stack
- Floating undo/redo buttons at the bottom-right of every authenticated page, hidden until
  an action is available, with Ctrl+Z / Ctrl+Shift+Z (and Ctrl+Y). They lift above other
  floating chrome (saved filters, floorplan floors, toasts, the page footer) rather than
  covering it. The floorplan editor's in-memory history drives the same buttons while that
  page is open
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
- **PWYW with a dynamic threshold**: same as above, but the role's features are gated by
  `threshold_met`, a stored flag comparing the pledge against the site's *current* cost-per-user
  (`services.admin.cost_tracking.cost_per_user()`, the same figure shown on `/costs/`) - the
  "pay over the running cost to get VIP" case. Recomputed on every successful charge and other
  qualifying Stripe webhook events, plus a nightly sweep (`sync_stripe_subscriptions`, all
  subscriptions, not per-user-anniversary) as a safety net for missed deliveries - so a pledge
  that used to clear the bar can silently stop granting access (Stripe's own `status` stays
  unchanged) within about a day even without a webhook. **Banked overpayment can extend access
  past that point**: a lump-sum or higher-than-threshold pledge accrues a usage-ledger runway
  (`has_banked_access`) that keeps granting the role's features for a time even after the current
  pledge stops clearing the threshold, or after the subscription is canceled outright - see
  `services/billing/banking.py`.
- A static PWYW minimum is also available as a non-dynamic alternative to the cost-per-user gate.

Managed from **Settings → Membership** (checkout, pledge updates, cancellation, and a link to
Stripe's hosted billing portal) and **Site Admin → Subscriptions** (per-role pricing). Stripe
webhooks (`/billing/webhooks/stripe/`) keep `RoleSubscription` status/pledge/threshold in sync;
a daily `sync_stripe_subscriptions` task re-syncs from Stripe as a safety net for missed
deliveries. `user_has_feature()`/`active_subscription_roles()` treat an active, threshold-met
paid subscription the same as an admin-issued grant. Service layer lives in `services/billing/`.

Billing infrastructure to reuse rather than rebuild:

- `services/billing/subscription_state.py` - the only writer of Stripe-owned `RoleSubscription`
  fields (`apply_subscription`, `mark_canceled`, `mark_past_due`). Each re-reads the row under
  `RoleSubscription.objects.locked(pk)`, ignores state older than `stripe_state_at` (an event's
  `created`, or a retrieve's send time via `retrieve_subscription`), and never moves a row out of
  `TERMINAL_SUBSCRIPTION_STATUSES` (`canceled`, `incomplete_expired`).
- `stripe_client.idempotency_key(kind, *parts)` - a Stripe idempotency key namespaced by
  `SITE_URL`, for any create against Stripe; `stripe_client.lock_billing_owner(user)` - the
  per-user row lock around customer creation and first sightings of new subscriptions;
  `stripe_client.configure()` - call at every entry point that reaches the SDK (the key is
  process-global).
- `services/billing/sync.py` - one `Subscription.list` page per task, then chunked retrieves of
  live rows no page reached (`RoleSubscriptionQuerySet.unsynced_since`).
- `RoleSubscriptionQuerySet.not_terminal()` (the rows the one-live-subscription constraint counts)
  and `ledger_advance_due()` (the PWYW rows whose ledger could move).
- Webhook payloads follow the endpoint's configured API version while SDK calls use the pinned one
  (`2026-06-24.dahlia`), so `webhooks._invoice_subscription_id` / `_charge_subscription_id` read
  both shapes.

What a role's `features` field can gate is any individual `SiteFeature`, not only broad tiers -
a site admin can bundle a single Private Pin panel behind its own paid role rather than an
all-or-nothing "premium" tier. Two examples: `SiteFeature.PROPERTY_OWNERS` restricts owner
names/contact info on the Property Records card (see above; the parcel/tax/assessment facts stay
free), and `SiteFeature.INCIDENT_HISTORY` restricts the deeper year-by-year Incident History panel
(see "Reported Incidents" above) while its sibling free panel is untouched.

## Media Storage & Serving

- **Every `/media/...` request is authenticated and authorized** by
  `dashboard.controllers.media.MediaGateView`, against a default-deny table keyed by the file's
  `upload_to` prefix (`services/media/access.py`). A family with no registered authorizer is
  refused, and `manage.py check` turns that into a startup error - so a new media field cannot
  ship without a read policy. See `docs/MEDIA_PIPELINE.md`. An avatar is served to whoever its
  profile is visible to (`authorize_avatar`, through `can_view_profile`), the rule
  `resolve_visible_identity` masks by; generated emoji avatars are open to every member.
- **Filesystem or object store**, chosen by `UL_MEDIA_STORAGE_BACKEND` (`filesystem` default,
  `s3` for Garage/MinIO/AWS). The switch changes nothing about who may read a file: `FileField.url`
  still returns `/media/...` and every read still passes the gate. `exports/`, `imports/` and
  `preview_sources/` stay on local disk either way.
- **An object store outage is a 503 the client can retry, not a 500** — the S3 client gives up
  inside Cloudflare's 100 s (`UL_S3_*_TIMEOUT_SECONDS`, `UL_S3_MAX_ATTEMPTS`), every upload path
  answers 503 with `Retry-After` and leaves nothing half-written, and processing an upload waits
  for storage (`services/media/upload_retry.py`) rather than removing it. See
  `docs/MEDIA_PIPELINE.md`, "Where the bytes are stored".
- **Four delivery paths, one authorization path** — `X-Accel-Redirect` to nginx off the media
  volume, `FileResponse` off disk, `X-Accel-Redirect` to an internal nginx proxy carrying a URL
  Django signed, or a stream from the object store through Django. Adding a fifth is a
  `MediaByteSource` subclass. See `docs/designs/media-object-storage.md`.
- **Uploads from their own origin** — `UL_MEDIA_BASE_URL` moves every media URL onto a separate
  hostname authenticated by a media-only signed cookie, so anything that slips past validation
  executes where there is no session cookie and no app data.
- **One profile's uploads are stored one at a time** — `services/media/storage.reserve_upload(profile,
  size)` holds a per-profile Postgres advisory lock for the transaction and admits the bytes against
  `SUM(file_size)` read under it; a second upload waits (20 s in a request, 120 s in an import task),
  then gets a 429. Duplicate-checksum lookups, caps and allowances that decide whether a row may be
  written go inside the block, next to the insert. D21 has the reasoning.
- **Upload size is capped to what the ingress will carry** — `UL_MAX_REQUEST_BODY_MB` lowers the
  site-wide limit, the import form's and the export-import view's, and feeds the browser's own
  pre-check, so an oversized file is refused before it is sent rather than by a proxy the app
  never hears from.

## Outbound Requests to User-Chosen Hosts

- **`request_public_url` / `open_public_url`** (`services/security/url_safety.py`) - the one way to
  send a request to a host a user or a stored row chose: any method, `params`/`json`/`data`, every
  hop resolved, pinned to the checked address and peer-checked, redirects followed by hand within
  `allowed_redirect_hosts` (`max_redirects=0` refuses all), credential headers dropped when a
  redirect changes host, a byte cap (`max_bytes`, or `read_limited` inside `open_public_url`), and a
  wall-clock `total_deadline` that cuts the sockets, header phase included. Refusals raise
  `UnsafeUrlError`/`RedirectRefusedError`; `ResponseTooLargeError` and `DeadlineExceededError` are
  `requests.RequestException`s. `fetch_public_url` is the streamed GET wrapper over the same loop,
  with no overall deadline. Used by UnifiedPush dispatch, the Immich gateway, the OAuth avatar
  download, the Wayback save and media materialisation.
- **`KeyedUpstreamSlots`** (`services/core/upstream_slots.py`) - a fleet-wide cap on concurrent
  upstream work per key (usually a profile), leased in the shared cache and failing open, beside the
  per-process `UpstreamSlots`. The Immich thumbnail routes hold one of each
  (`controllers/immich.immich_thumbnail_response`) and are throttled per account.
- **`start_drip_server`** (`core/tests/slow_servers.py`) - a loopback server that answers a byte at a
  time, for testing a deadline against real sockets.

## Shared Infrastructure for Views, Tasks and Upstreams

- **`RequestUpstream`** (`services/core/request_upstream.py`) - the one policy for calling an upstream
  while a user waits: cache (`bounded_cache`), then a per-account `throttle.allow` on misses only, then a
  non-blocking slot, then a deadline. The slot is held by the fetching thread, so a fetch the request
  abandoned still counts until it returns, and it still caches what it gets; a raised error is never
  cached. `start()` + `wait_all()` run several under one budget. Each upstream is a subclass in
  `services/apis/request_upstreams.py` with its own slots, deadline and rate; `refusal_json` maps an
  unanswered result to 429/502/503 with `Retry-After`. Used by place search/resolve/details, the Places
  layer (`services/map/nearby_places.py`), trip forecasts, historical-map browse, the REData media proxies
  and Flickr album lookup.
- **`call_with_deadline` / `submit_bounded`** (`services/core/timeout_utils.py`) - run a blocking call on a
  small shared pool and stop waiting after a wall-clock budget (the call itself cannot be killed and runs
  on); `submit_bounded` returns the future for callers that wait on several or must know whether an
  abandoned call ever started. Each run closes its DB connections.
- **`UpstreamSlots`** (`services/core/upstream_slots.py`) - a per-process, never-blocking cap on in-flight
  fetches to one upstream; subclass it and implement `limit()`. The tile proxies and `RequestUpstream`
  build on it; `KeyedUpstreamSlots` (above) is the fleet-wide per-key form.
- **`throttled` / `allow` / `account_or_address`** (`services/security/throttle.py`) - a fixed-window
  per-caller rate limit: `throttled(scope, Rate(...), methods=..., identify=...)` wraps a view and answers
  429 with `Retry-After`; `allow()` is the same check for code that is not a view. Fails open when the
  cache is down. The scope and rate are readable off the URLconf.
- **`bounded_cache`** (`services/core/bounded_cache.py`) - reads and writes against
  `settings.PROXIED_BYTES_CACHE` that treat an unreachable or full cache as a miss rather than an error,
  and `set_if_small` to refuse bodies over a ceiling while still serving them.
- **`read_capped`** (`services/core/gateway.py`) - read a `stream=True` response up to a byte ceiling,
  refusing (not truncating) anything larger and refusing a response that was not streamed. A read that
  fails midway raises `GatewayRequestError` like a refusal does, and a response refused for its size is closed.
- **`reorder_id_ceiling`** (`services/core/reorder_limits.py`) - the most ids a drag-and-drop reorder may
  name: the container's own item limit, or that setting's validator maximum when it is unlimited.
- **`UpstreamBreaker`** (`services/core/upstream_breaker.py`) - an upstream that tells this deployment
  to wait opens a breaker held in the shared cache, and until it closes every process refuses the
  call without a request, raising `UpstreamThrottledError` and logging `was_rate_limited=True`.
  `RedataBreaker` opens REData's throttle pools and its busy sources; `WaybackBreaker` opens the
  Internet Archive host that answered a 429 (archive.org for lookups, web.archive.org for saves).
  A new upstream subclasses it and joins `BREAKERS`.
- **`beat_lock` / `acquire_lock` / `release_lock`** (`services/core/locks.py`) - a named overlap lock in
  the cache for scheduled sweeps; release deletes the key only while the caller's token still holds it.
- **Log redaction** (`UrbanLens/logging_filters.py::SecretRedactionFilter`, `services/security/redact.py::redact_urls`)
  - every handler in `LOGGING`, and every handler a Celery worker installs, rewrites the URLs a record prints, in the
  message and the traceback: a credential parameter becomes a `redact_secret` token, a coordinate parameter or
  `lat,lng` value a `redact_coordinate` token, and a `user:password@` password a token. A `requests` error, whose
  text is its URL, can be logged as it is (P203). Any email address in the message or the traceback becomes an
  `<email:...>` token (`redact_email_addresses`): an SMTP refusal's text names the recipients it refused.
- **`mark_retry_later(request)`** (`UrbanLens/logging_filters.py`) - for a view whose 503 with `Retry-After` means
  "not yet": `django.request` drops that request's 503 instead of logging it at ERROR. media-copy uses it (P204).

## Background Work (Celery)

- **`safely_enqueue_task`** (`services/core/celery.py`) - the one way to queue a task. When the
  broker refuses a message it writes a `TaskOutboxEntry` in the caller's transaction (`durable=True`,
  the default), and `drain_task_outbox` (beat, every minute) queues it again, keeping its remaining
  `countdown`, its `expires` and its queue. A caller that handles `None` itself (reports the
  failure, runs inline, releases a claim, or is a sweep that will find the work again) passes
  `durable=False`. `bin/check_enqueue_durability.py` (manual pre-commit hook) fails any caller
  that uses the result without choosing.
- **Per-queue time limits** (`services/core/task_limits.py`) - a task that declares no
  `soft_time_limit`/`time_limit` gets its queue's (`QueueTimeLimits`, the `task_annotations`
  setting): interactive 120/150 s, panel 110/130, ai 90/120, sandbox 720/780, maintenance
  2700/3000, other batch queues the global 2700/3600. Startup check `dashboard.E013` fails a task
  whose limits are missing, inverted, or above its queue's ceiling (interactive 300 s; batch queues
  the broker visibility timeout less ten minutes).
- **A soft limit `except Exception` cannot swallow** - `UrbanLensTask` (the app's `task_cls`)
  replaces billiard's soft-limit signal handler, in prefork children, with one raising
  `TaskSoftTimeLimit`, a `BaseException`, and turns it back into `SoftTimeLimitExceeded` at the task
  boundary. Code that cleans up on a soft limit catches `SOFT_TIME_LIMIT_ERRORS`.
- **`pk_ranges` / `dispatch_pk_ranges`** (`services/core/celery.py`) - keyset-page a queryset into
  `(first_pk, last_pk)` ranges and queue one subtask per range, holding one chunk of keys at a time.
  Used by `sweep_achievements`, `sweep_reputation` and `backfill_achievement`.
- **Stall sweeps** that recover work a lost enqueue or a dead worker dropped, each keyed on a
  marker set in the same transaction as the change: `requeue_stalled_device_scans` (PENDING/
  PROCESSING uploads), `sweep_stale_fact_confidence` (`Fact.needs_recompute`),
  `requeue_pending_calendar_pushes` (`TripCalendarLink.push_requested_at`: an auto-sync change, or an export the budget cut short;
  and `CalendarEventDeletion`: an event whose stop or trip was deleted),
  `sweep_unarchived_links` (a pin or wiki link without a Wayback snapshot: a failed URL waits
  1 h, 6 h, 1 d, 3 d, 7 d, then 30 d before it is given up, in `wayback_retry_at`; a link whose
  own task never ran is taken up after 15 minutes; `services/links/wayback_archive.py`).
- **Retention sweeps** (`services/core/retention.py`, nightly) - `prune_expired_sessions`
  (`clearsessions`) and `prune_read_notifications`, deleting in bounded primary-key batches. The period is
  `SiteSettings.notification_retention_days` ("Data retention" in the Django admin; 0 keeps rows for ever).
  Device scans are never deleted, and an account's scans outlive it with `profile` cleared (Jess, 2026-09-29).

## Site Administration

- The api-limits page also shows a **REData capabilities** card - every domain the connected
  REData instance can answer, its endpoint, prewarm status, and providers (with billable/pinned
  flags), generated from REData's own registries and cached for an hour
- `/site-admin/` panel: user management, site-wide settings, usage stats (KPIs, system, API),
  subscription role management, per-service API rate-limit toggles, plugin inventory,
  achievement definitions, cost tracking, UI component showcase, dev toolbar (theme/map-dark-mode
  toggles, session reset)
- Data export/import tooling and on-demand/scheduled database backups
- **Per-account row limits** (`services/core/capacity.py`) — `SiteSettings` caps on saved filters,
  pin lists, personal labels, custom fields, active push devices and photos per album (`0` =
  unlimited). Every add goes through `reserve(capacity, owner_pk, adding=n)`, which serialises one
  owner's adds on an advisory lock, counts, and raises `CapacityExceededError` (`user_message` for
  the response); `reserve_each` for several owners, `ensure_room` for an unlocked early refusal
  before an upload, `Capacity.ceiling()` to bound a posted id list. Import steps skip the overflow
  with one warning; undo restores raise `UndoExpiredError` via `undo.base.restore_capacity`
- Subscription roles grant feature flags (`SiteFeature`) per user; pending grants can attach to an
  email invite for users who haven't joined yet. Site Admin → Subscriptions lists every active grant
  with who made it and when, and any site admin can change or revoke any of them
- `/health/` returns a liveness response for Docker healthchecks and load-balancer probes
  (`controllers/health.py`, `AllowAny`) - the compose stack gates `app`/`app-ws`/`nginx` startup on it
- `/health/ready` and `/health/primary` reuse the migration state (30s) and connection count (5s) per
  process through `services/core/process_memo.ProcessMemo`, a generic per-process TTL memo that holds
  nothing its `keep` predicate refuses; database and cache reachability are probed on every call
- Deployment configuration fails closed at import (`settings/_env.require_deployment_setting`):
  outside local/development/testing, a missing `DJANGO_SECRET_KEY`, `UL_SITE_URL` (or a loopback
  one), broker or Dragonfly URL, or `DJANGO_DEBUG=true`, refuses to start. `UL_ENVIRONMENT` is
  resolved once by `environments.meta.environment_from_env`; unset or blank refuses to start (Jess, 2026-10-07),
  except in a test run, which is `testing`. See P156.
- `services/core/site_urls.absolute_url(path)` builds request-less links (mail, SMS, Celery) on
  `SITE_URL`; nothing else may join onto it (`test_site_urls.py`)
- `/thanks/` credits page, rendering live contributor data pulled from the GitHub API
  (`controllers/thanks.py` via `services/apis/infra/github/contributors.py`)

## AI Integration

- Pluggable AI provider gateway (OpenAI, Cloudflare, Anthropic). The AI chat
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

## AI Assistant

- A global, tool-calling chat assistant reachable from every authenticated page - a floating
  bottom-right button and the `?`/Shift+`?` hotkey open an overlay (`window.location.pathname`
  is sent along, resolved server-side into page context under the same access rules the real
  page's own view would apply); `/assistant/` remains as a deep link / no-JS fallback. Gated
  behind a site-wide flag, a per-user entitlement, and a per-profile `ai_enabled`/
  `external_apis_enabled` pair - an ungated user sees no button, no hotkey binding, and every
  turn endpoint 404s
- Runs entirely off an isolated sandbox tier (`ai-worker` draining its own Celery queue, plus a
  Django-free `ai-inference` service holding only provider API keys), never inline in the web
  process, and never reachable to REData or the open web except through an allowlisting egress
  proxy - see `docs/AI_PIPELINE.md` for the full architecture and threat model
- A typed registry of read-only tools the model can call, each scoped to what the requesting
  profile can already see: search/list the user's own pins and unvisited pins; list/create trips
  and add trip activities; look up page help and recently-dismissed onboarding
  explainers/tooltips (and reopen one on request); peek and undo the user's last undoable action;
  straight-line distance and (when routing succeeds) drive time between two of the user's pins or
  coordinates; a weather forecast for one of the user's pins or coordinates; evidence of tunnels
  or below-grade levels at a pin (floorplan, photo captions/labels, wiki comments); and whether
  the user has visited a pin (a confirmed logged visit vs. a merely-nearby GPS track or pending
  visit suggestion, never conflated)
- Every write the assistant can perform (creating a trip, adding a trip activity, undoing the
  last action) is proposal-then-confirm: the model can never execute a write directly, only
  produce a confirm button whose action is bound server-side to the caller's own profile
- Answers are grounded, never fabricated: page-help text comes from a static per-page dictionary,
  dismissed-explainer text is exactly what the user's own page rendered (captured client-side,
  re-validated server-side), and every tool result the model sees is scanned and delimited before
  being added to its context
- Turns run asynchronously (enqueue-then-poll) on both the website and the external API - see
  `docs/EXTERNAL_API.md`'s "AI Assistant" section for the wire protocol

## REST API

Two separate DRF surfaces - see `dashboard/urls.py` and `dashboard/external_api/__init__.py`
for the boundary rationale:

- **Internal, session-authenticated**, under `/dashboard/rest/`. Deliberately minimal - only what
  the app's own frontend uses: `pins` (PATCH/DELETE only - pin creation goes through
  `MapController.post_add_pin` instead, not this router) and a `reviews`
  `create_or_update` action for the star-rating widget.
- **External, API-key-authenticated**, under `/dashboard/api/external/v1/`. Lets a third-party
  application act on a user's behalf with a scoped grant - see "External API keys" under Account
  & Auth below. Independently versioned and never shares serializers/viewsets with the internal
  surface.

## Direct Messaging

- End-to-end encrypted 1:1 direct messages and named group chats
- **Group-chat scope (deliberate, as of 2026-07-18; reactions added 2026-07-29):** group chats
  support text (plaintext or E2EE), pin sharing (one provenance-tracked PinShare per member),
  rename, creator-managed membership, per-member mute, unread tracking, and **message reactions**
  (`Reaction.group_message`, with live WebSocket broadcast - reachable via the external/mobile API
  today; the web UI's group-chat thread has no reaction picker or live-update handler yet, unlike
  the 1:1 thread's). They still intentionally do *not* have 1:1 parity for: image attachments,
  replies/quotes, map attachments, coordinate/address detection, disappearing messages, typing
  indicators, read receipts, or delete-for-self (only the sender's delete-for-everyone exists). A
  group whose creator leaves becomes permanently unmanaged (no ownership transfer). Extending any
  of these is a product decision, not a bug fix.
- Group size (`SiteSettings.max_group_chat_members`) and how many groups one person may be in
  (`max_group_chats_per_user`) are admin settings, enforced when a group is created or extended
- The inbox is `services/messaging/inbox.py:InboxFeed`: one SQL `UNION` of the per-partner DM
  aggregate and the annotated group memberships (`group_inbox_rows`), ordered by last activity and
  sliced in the database, with only the slice built into conversation dicts. The sidebar lists 50
  with "Show more"; the dropdown and the external API take their slice from it
- Rich compose toolbar: image attachment, share location/map, share pin, @mention, emoji. The
  map composer dialog has two tabs - draw a new map, or choose one of your existing maps (search
  by title) - both attach the same way
- Fallback (initial-letter) avatars use a deterministic per-person color that's guaranteed
  distinct from everyone else shown in the same list (e.g. a group chat's member dialog), so two
  people without photos never look identical there
- Read receipts, online status indicator, typing indicator (visibility of each configurable per user). Online status is per-socket membership renewed on the socket heartbeat, so a socket whose worker died stops counting within 15 minutes
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

- The Channels layer has its own Dragonfly (`channel-layer` in compose, `UL_CHANNEL_LAYER_URL`),
  falling back to the shared store where it is not provisioned - D22
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

## Concurrency, limits and locks (shared infrastructure)

Reuse these rather than hand-rolling a counter, a lock or a check-then-insert.

- **Counters** - `services/core/counters.py`: `hit(key, ttl, on_outage=..., sliding=...)`, `peek`,
  `refund`, `clear`. One atomic Lua call on Dragonfly (`ResilientRedisCache.incr_window`), one
  locked step in tests (`AtomicLocMemCache`). The caller picks `Outage.REFUSE` (raise; for limits
  guarding an upstream's budget) or `Outage.LOCAL` (count in-process for the outage). The request
  throttle, login/2FA lockout and the WebSocket frame/fanout/message budgets all use it.
- **Tallies** - `counters.add_to_tally(key, {field: n}, ttl)` and `take_tally(key)`: named counts
  under one hash key, read and emptied in one step; no local fallback. The tallied call ledger uses it.
- **Store operations that do not guess** - `core/cache_backend.py`: `AtomicCacheOps`
  (`incr_window`, `peek_int`, `decr_if_positive`, `delete_if_value`, `add_to_tally`, `take_tally`) raise
  `CacheUnavailableError` (a `ValueError`) instead of answering as an empty cache.
- **Inbound throttle** - `services/security/throttle.py`: `throttled(scope, Rate(limit, window,
  on_outage=...), methods, identify=account_or_address)` wraps a URLconf entry; the wrapper exposes
  `throttle_scope`/`throttle_rate`/`throttle_methods`/`throttle_identify` so tests can assert a route
  is guarded.
- **Overlap locks** - `services/core/locks.py`: `acquire_lock`/`release_lock`/`beat_lock`; release
  is an atomic compare-and-delete, so an overrunning holder never drops its successor's lock.
- **Live connections** - `services/core/connection_registry.py`: `ConnectionRegistry`, a sorted set
  per identity that forgets unrenewed members. Backs the per-account socket allowance and DM
  presence.
- **Paid-API reservation** - `rate_limiter.api_call_slot(service, endpoint=...)`, for calls outside
  a gateway session.
- **Trip roster** - `services/trips/trip_seats.py`: `reserve_trip_seat` / `lock_trip_roster`, the
  only way a trip's roster grows under `max_trip_members`.
- **Guarded transitions** - pin-share accept/reject settle once (`apply_pin_share_response`);
  `Friendship.accept()` locks both profiles; `apply_wiki_edit(..., base_revision_id=...)` refuses
  an edit over a newer write (`WikiEditConflictError`, 409); `share_markup_map()` is the one
  map-share path (one row per map and pair).
- **Client: one request at a time** - `frontend/ts/shared/single-flight.ts`: `singleFlight(task)`;
  calls made mid-flight share one trailing run, so an older response never lands last. Wraps the
  map's full pin refresh (`window._refreshAllPins`). Hand the wrapper to other bundles rather than
  wrapping twice: each bundle carries its own copy of the module.
- **Client: background polling** - `frontend/ts/shared/poller.ts`: `startPoller(tick, {intervalMs,
  element, immediate})`, also `window.ulStartPoller` for inline scripts. No overlapping ticks,
  paused while the tab is hidden, stops once `element` leaves the document. A bare `setInterval`
  poll fails `poller.contract.test.ts`.

## Games: shared infrastructure

- `services/core/session_access.SessionAccess[S]` - the one "is this profile an active participant
  of this session" rule for every participant-session game, backed by each participant queryset's
  `active()`. Controllers use `controllers.games.participant_session_or_404`, consumers
  `_session_access()`, and `SessionChat.send` enforces it itself
- `services/core/session_chat.SessionChat[S, M]` - session chat send/history
- `services/games/glicko2.py` (rating math) and `models/abstract/ratings.py` (Glicko-2 defaults and
  the `Glicko2RatingFields` display-scale mixin), shared by SpotGuessr and Trivia
- **Friend invite picker** - `GET /games/friends/?exclude=<ids>` (`controllers.games.GameFriendPickerView`)
  renders friends as `ul-checkbox` inputs named `invite_profile_ids`. Pages include
  `partials/games/_friend_picker.html` (htmx-loaded); `frontend/ts/shared/friend-picker.ts` reads the
  ticked ids, offers a retry on a failed load, and builds the mid-game "invite more" dialog

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
  participant (an invited-but-not-yet-accepted player never gates this) — no exceptions. A short
  prewarm cache (`services/spotguessr/prewarm.py`, up to 20 minutes) may pre-pick a location ahead
  of when it's shown, but only after it already passed this same eligibility check
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
- **Multiplayer stall handling and leave/kick**: a round stuck because a participant went AFK is
  force-revealed by a Celery beat sweep after 10 minutes (marking the session `ABANDONED` if
  literally nobody guessed), and the host can end an in-progress or not-yet-started game (cancel
  the lobby) immediately from an "End game" control. Any participant can leave, from the lobby or
  mid-game, or decline an invite; the host can remove anyone else from the lobby roster
  (`spotguessr.leave`/`spotguessr.kick`, `services.spotguessr.session.leave_session`/`kick_participant`).
  The host role passes to the earliest remaining joined player if the host leaves, the session is
  `ABANDONED` once nobody joined is left, and a round only the departed player was holding up is
  revealed at once. A departed player (`GameSessionParticipantStatus.LEFT`) loses every route back
  in - HTTP, WebSocket connect, chat - until the host invites them again. One who left or was
  removed once the game was under way stays on the final scoreboard with their points, marked
  "Left" or "Removed" and ranked after everyone who finished, and keeps the game in their session
  history (`departure`, `played()`/`in_history()`); a declined invite or a lobby departure does neither
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
  buildings; a year only when REData says it is the building's own, withdrawn once the records stop
  saying so), **AI-generated** from wiki articles with substantial content (up to 3 per wiki),
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
  transfers automatically if the host themselves leaves. A departed player loses every route
  back in (HTTP, WebSocket connect, chat send) until the host invites them again; one who left
  or was removed mid-game stays on the final scoreboard, marked "Left" or "Removed", as in SpotGuessr

Not yet built: a moderation review UI for AI-rejected questions (the only way to inspect why a
question was rejected today is direct DB access) - explicitly decided against, not just
unbuilt - see the design doc's "Known gaps" section.


## Games: Consensus

The wiki-data-completion game, and the only game that writes back to shared data. A round shows a
player one missing or unconfirmed piece of data about a `Wiki` they have a visited pin for — a
name, description, alias, or a photo's coordinates — and the player supplies it. The per-field-kind
registry driving round generation and answer application lives in `services/consensus/fields.py`,
so a new answerable field is a registry entry rather than new game code.

- **Solo and competitive modes.** Solo applies an answer to the live wiki the instant it is
  submitted, no trust check. Competitive mode races participants and resolves disagreement by a
  plain one-vote-per-participant tally, applying the winning value the same unconditional way the
  instant the round resolves; an unsettled vote is meant to land in a cross-session *tentative*
  pool that later sessions can confirm (`ConsensusTentativeAnswer`), but **nothing currently
  promotes a tentative row to applied** - `record_tentative_answers` accumulates `support_count`
  across sessions, and the `PENDING → APPLIED/DISMISSED` lifecycle the model defines is otherwise
  dead code (no controller, task, or admin path ever drives it past `PENDING`). An unsettled
  disagreement today just accumulates support forever rather than ever resolving. See
  `docs/audits/FEATURES_CODE_AUDIT.md`'s Consensus section for the open question this raises.
- **Trust, tracked but not yet gating the write.** `ConsensusProfile` carries a real Beta-Bernoulli
  posterior (`trust_alpha`/`trust_beta`) updated from trust-check rounds — rounds whose answer is
  already known — starting from a weakly-informative prior so a new player is neither trusted nor
  distrusted. That posterior is genuinely consulted in two places: how often a trust-check round is
  injected, and the confidence weight attached to a `FactEvidence` row (a separate metadata layer
  read by AI article-writing and Consensus's own recheck-round picker). It does **not** currently
  weight or gate which answers get applied to the live wiki fields (`Wiki.name`/`description`/etc.)
  - a brand-new or actively-distrusted account's answer overwrites those fields with exactly the
  same immediacy as a maximally-trusted veteran's. Points and levels are Consensus-only and
  deliberately not shared with SpotGuessr/Trivia's Glicko-2 ratings, and are awarded for out-of-game
  manual wiki edits too (`models/wiki_edit/signals.py`)
- **Session flow** under `games/consensus/`: home, start, lobby, invite, join, begin,
  round, answer, vote, end — with `ws/consensus/session/<id>/` pushing round and resolution
  updates, and a stall sweep (`sweep_stalled_consensus_sessions`) reclaiming abandoned sessions
- Answers feed the same fact-confidence machinery documented under the wiki sections
  (`services/facts/confidence.py`), which converges a value by trust-weighted agreement clustering
